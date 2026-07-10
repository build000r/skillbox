from __future__ import annotations

import json
import socket
import sys
import threading
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Callable
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import runtime_ops  # noqa: E402
from runtime_manager import text_renderers  # noqa: E402

PULSE_MODULE = SourceFileLoader(
    "skillbox_pulse_mcp_healthcheck_tests",
    str((ENV_MANAGER_DIR / "pulse.py").resolve()),
).load_module()


class _OneShotTcpServer:
    def __init__(self, handler: Callable[[socket.socket], None]) -> None:
        self._handler = handler
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self.port = int(self._sock.getsockname()[1])
        self.error: BaseException | None = None

    def __enter__(self) -> "_OneShotTcpServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=1)
        if self.error is not None:
            raise AssertionError(f"fake MCP server failed: {self.error}") from self.error

    def _serve(self) -> None:
        try:
            conn, _addr = self._sock.accept()
        except OSError:
            return
        try:
            with conn:
                conn.settimeout(1)
                self._handler(conn)
        except BaseException as exc:
            self.error = exc


def _healthcheck(port: int, *, timeout_seconds: float = 0.2) -> dict[str, object]:
    return {
        "type": "mcp_ready",
        "transport": "tcp",
        "host": "127.0.0.1",
        "port": port,
        "timeout_seconds": timeout_seconds,
    }


def _service(port: int, *, timeout_seconds: float = 0.2) -> dict[str, object]:
    return {
        "id": "fake-mcp",
        "kind": "mcp-bridge",
        "command": "true",
        "healthcheck": _healthcheck(port, timeout_seconds=timeout_seconds),
    }


def _initialize_response() -> bytes:
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": runtime_ops.MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "serverInfo": {"name": "fake-mcp", "version": "test"},
                },
            }
        )
        + "\n"
    ).encode("utf-8")


class McpReadyHealthcheckTests(unittest.TestCase):
    def test_healthy_bridge_completes_initialize_under_budget(self) -> None:
        def handler(conn: socket.socket) -> None:
            request = conn.recv(4096).decode("utf-8", errors="replace")
            self.assertIn('"method":"initialize"', request)
            conn.sendall(_initialize_response())

        with _OneShotTcpServer(handler) as server:
            started = time.monotonic()
            state = runtime_ops.service_healthcheck_state(_service(server.port))

        self.assertEqual(state["state"], "ok")
        self.assertEqual(state["healthcheck_type"], "mcp_ready")
        self.assertEqual(state["healthcheck_transport"], "tcp")
        self.assertEqual(state["protocol_version"], runtime_ops.MCP_PROTOCOL_VERSION)
        self.assertLess(state["elapsed_ms"], 200)
        self.assertLess((time.monotonic() - started) * 1000, 200)

    def test_wrong_process_on_port_fails_bad_handshake(self) -> None:
        def handler(conn: socket.socket) -> None:
            conn.recv(4096)
            conn.sendall(b"not json\n")

        with _OneShotTcpServer(handler) as server:
            state = runtime_ops.service_healthcheck_state(_service(server.port))

        self.assertEqual(state["state"], "down")
        self.assertEqual(state["reason"], "bad_handshake")
        self.assertIn("non-json", state["protocol_error"])

    def test_silent_listener_fails_no_response(self) -> None:
        def handler(conn: socket.socket) -> None:
            conn.recv(4096)
            time.sleep(0.12)

        with _OneShotTcpServer(handler) as server:
            state = runtime_ops.service_healthcheck_state(_service(server.port, timeout_seconds=0.05))

        self.assertEqual(state["state"], "down")
        self.assertEqual(state["reason"], "no_response")
        self.assertLess(state["elapsed_ms"], 200)

    def test_refused_connection_reports_refused(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])

        state = runtime_ops.service_healthcheck_state(_service(port, timeout_seconds=0.05))

        self.assertEqual(state["state"], "down")
        self.assertEqual(state["reason"], "refused")

    def test_status_and_doctor_render_mcp_ready_declarations(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        service = _service(port, timeout_seconds=0.05)
        model = {"root_dir": str(ROOT_DIR), "env": {}, "logs": [], "services": [service]}

        with mock.patch("runtime_manager.runtime_ops.ownership_state_for_service", return_value="covered"):
            [status] = runtime_ops._runtime_service_statuses(model)
        rendered = text_renderers._format_service_line(status)
        doctor = runtime_ops.validate_mcp_healthchecks(model)

        self.assertEqual(status["healthcheck_type"], "mcp_ready")
        self.assertEqual(status["healthcheck_transport"], "tcp")
        self.assertIn("health mcp_ready/tcp (refused)", rendered)
        self.assertEqual(doctor[0].code, "mcp-healthchecks")
        self.assertEqual(doctor[0].details["services"][0]["healthcheck_type"], "mcp_ready")

    def test_pulse_restarts_unhealthy_mcp_bridge_with_existing_backoff(self) -> None:
        service = {
            "id": "fake-mcp",
            "kind": "mcp-bridge",
            "required": True,
            "command": "fake-mcp",
            "healthcheck": {
                "type": "mcp_ready",
                "transport": "tcp",
                "host": "127.0.0.1",
                "port": 65534,
            },
        }
        state = PULSE_MODULE.PulseState()
        state.service_states["fake-mcp"] = "running"
        state.unhealthy_since["fake-mcp"] = 0.0
        probe = {
            "state": "starting",
            "pid": 12345,
            "healthcheck_type": "mcp_ready",
            "reason": "bad_handshake",
        }

        with (
            mock.patch.object(PULSE_MODULE, "_restart_with_backoff", return_value=True) as restart,
            mock.patch.object(PULSE_MODULE, "log_runtime_event"),
            mock.patch.object(PULSE_MODULE, "log"),
        ):
            PULSE_MODULE._reconcile_pulse_service(
                {"services": [service]},
                state,
                {"fake-mcp": service},
                "fake-mcp",
                probe,
                auto_restart=True,
                unhealthy_grace_seconds=1.0,
                now=5.0,
            )

        restart.assert_called_once()
        self.assertEqual(restart.call_args.kwargs["reason"], "unhealthy_http")
        self.assertEqual(state.service_states["fake-mcp"], "running")
        self.assertNotIn("fake-mcp", state.unhealthy_since)


if __name__ == "__main__":
    unittest.main()
