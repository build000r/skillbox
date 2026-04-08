#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
import json
import signal
import socket
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "cm"
SERVER_VERSION = "http-bridge"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bridge cm's HTTP-only MCP server onto stdio JSON-RPC.",
    )
    parser.add_argument(
        "--cm-command",
        default="cm",
        help="Path to the cm binary.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind the cm HTTP MCP server to.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=3222,
        help="Port for the cm HTTP MCP server.",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for the HTTP server to start.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for each HTTP MCP request.",
    )
    return parser.parse_args()


def tcp_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    body = json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, method="POST")
    request.add_header("content-type", "application/json")
    request.add_header("accept", "application/json")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8").strip()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"non-JSON response: {raw}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"unexpected response type: {type(parsed).__name__}")
    return parsed


def wait_for_server(host: str, port: int, timeout: float, proc: subprocess.Popen[str] | None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tcp_ready(host, port):
            return
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"cm exited with code {proc.returncode} before serving HTTP MCP")
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for cm HTTP MCP on {host}:{port}")


def emit_error(message_id: Any, message: str) -> None:
    if message_id is None:
        return
    sys.stdout.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32000, "message": message},
            }
        )
        + "\n"
    )
    sys.stdout.flush()


def emit_result(message_id: Any, result: dict[str, Any]) -> None:
    if message_id is None:
        return
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()


def initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def normalize_response(message_id: Any, response: dict[str, Any]) -> dict[str, Any]:
    if "result" in response or "error" in response:
        merged = dict(response)
        merged.setdefault("jsonrpc", "2.0")
        if message_id is not None:
            merged["id"] = message_id
        return merged
    return {"jsonrpc": "2.0", "id": message_id, "result": response}


def main() -> int:
    args = parse_args()
    http_url = f"http://{args.host}:{args.port}"
    child: subprocess.Popen[str] | None = None

    if not tcp_ready(args.host, args.port):
        child = subprocess.Popen(
            [args.cm_command, "serve", "--host", args.host, "--port", str(args.port)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=sys.stderr,
            text=True,
        )

    def _shutdown(*_args: object) -> None:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                child.kill()
        raise SystemExit(0)

    atexit.register(lambda: child is not None and child.poll() is None and child.terminate())
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        wait_for_server(args.host, args.port, args.startup_timeout, child)
    except RuntimeError as exc:
        emit_error(1, str(exc))
        return 1

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        message_id: Any = None
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("request must be a JSON object")
            message_id = payload.get("id")
            method = str(payload.get("method") or "")
            if method == "initialize":
                emit_result(message_id, initialize_result())
                continue
            if method == "notifications/initialized":
                continue
            if method == "ping":
                emit_result(message_id, {})
                continue

            response = post_json(http_url, payload, timeout=args.request_timeout)
            if response is not None and message_id is not None:
                sys.stdout.write(json.dumps(normalize_response(message_id, response)) + "\n")
                sys.stdout.flush()
        except Exception as exc:  # noqa: BLE001
            emit_error(message_id, str(exc))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
