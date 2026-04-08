#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def sort_routes(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        routes,
        key=lambda route: (
            0 if str(route.get("match") or "exact") == "exact" else 1,
            -len(str(route.get("path") or "")),
            str(route.get("path") or ""),
            str(route.get("id") or ""),
        ),
    )


class RouteStore:
    def __init__(self, route_file: Path) -> None:
        self._route_file = route_file
        self._lock = threading.Lock()
        self._mtime_ns: int | None = None
        self._listeners: dict[str, list[dict[str, Any]]] = {"public": [], "private": []}
        self._last_error = ""

    def maybe_reload(self) -> None:
        try:
            stat = self._route_file.stat()
        except FileNotFoundError:
            with self._lock:
                self._mtime_ns = None
                self._listeners = {"public": [], "private": []}
                self._last_error = ""
            return

        if self._mtime_ns == stat.st_mtime_ns:
            return

        payload = json.loads(self._route_file.read_text(encoding="utf-8"))
        listeners: dict[str, list[dict[str, Any]]] = {"public": [], "private": []}
        for route in payload.get("routes") or []:
            listener = str(route.get("listener") or "public").strip().lower() or "public"
            listeners.setdefault(listener, []).append(dict(route))
        listeners = {
            listener: sort_routes(items)
            for listener, items in listeners.items()
        }

        with self._lock:
            self._mtime_ns = stat.st_mtime_ns
            self._listeners = listeners
            self._last_error = ""

    def routes_for(self, listener: str) -> list[dict[str, Any]]:
        try:
            self.maybe_reload()
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            return []
        with self._lock:
            return [dict(item) for item in self._listeners.get(listener, [])]

    def health_payload(self) -> dict[str, Any]:
        try:
            self.maybe_reload()
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
        with self._lock:
            return {
                "ok": not bool(self._last_error),
                "route_file": str(self._route_file),
                "last_error": self._last_error,
                "routes": {
                    listener: len(items)
                    for listener, items in self._listeners.items()
                },
            }


def path_matches(route: dict[str, Any], request_path: str) -> bool:
    route_path = str(route.get("path") or "").strip() or "/"
    match = str(route.get("match") or "exact").strip().lower() or "exact"
    if match == "exact":
        return request_path == route_path
    if route_path == "/":
        return True
    if request_path == route_path:
        return True
    normalized = route_path.rstrip("/")
    return request_path.startswith(f"{normalized}/")


class IngressServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        listener_name: str,
        route_store: RouteStore,
    ) -> None:
        self.listener_name = listener_name
        self.route_store = route_store
        super().__init__(server_address, handler_class)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "skillbox-ingress"

    def do_GET(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def do_OPTIONS(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: Any) -> None:
        print(
            f"[{self.server.listener_name}] {self.client_address[0]} "
            f"{self.command} {self.path} :: {format % args}",
            flush=True,
        )

    def _handle(self) -> None:
        if self.path == "/__skillbox/health":
            self._write_health()
            return

        request_path = urlsplit(self.path).path or "/"
        route = self._match_route(request_path)
        if route is None:
            self._write_text(404, "No ingress route matched this request.\n")
            return

        upstream = str(route.get("upstream_base_url") or "").strip()
        if not upstream:
            self._write_text(502, f"Ingress route {route.get('id', '(unknown)')} has no upstream.\n")
            return

        parsed = urlsplit(upstream)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            self._write_text(502, f"Ingress route {route.get('id', '(unknown)')} has an invalid upstream.\n")
            return

        body = self._read_body()
        target = self.path or "/"
        headers = self._forward_headers(parsed.netloc)
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection: http.client.HTTPConnection | http.client.HTTPSConnection | None = None
        try:
            connection = connection_type(parsed.netloc, timeout=30)
            connection.request(self.command, target, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
        except Exception as exc:
            self._write_text(502, f"Upstream request failed: {exc}\n")
            return
        finally:
            try:
                if connection is not None:
                    connection.close()
            except Exception:
                pass

        self.send_response(response.status, response.reason)
        for header, value in response.getheaders():
            lower = header.lower()
            if lower in HOP_BY_HOP_HEADERS or lower == "content-length":
                continue
            self.send_header(header, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD" and payload:
            self.wfile.write(payload)

    def _match_route(self, request_path: str) -> dict[str, Any] | None:
        for route in self.server.route_store.routes_for(self.server.listener_name):
            if path_matches(route, request_path):
                return route
        return None

    def _read_body(self) -> bytes | None:
        content_length = str(self.headers.get("Content-Length") or "").strip()
        if not content_length:
            return None
        try:
            length = int(content_length)
        except ValueError:
            return None
        if length <= 0:
            return None
        return self.rfile.read(length)

    def _forward_headers(self, upstream_host: str) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in self.headers.items():
            lower = key.lower()
            if lower in HOP_BY_HOP_HEADERS or lower == "host":
                continue
            headers[key] = value
        forwarded_for = self.client_address[0]
        existing_forwarded_for = str(self.headers.get("X-Forwarded-For") or "").strip()
        if existing_forwarded_for:
            forwarded_for = f"{existing_forwarded_for}, {forwarded_for}"
        headers["Host"] = upstream_host
        headers["X-Forwarded-For"] = forwarded_for
        headers["X-Forwarded-Host"] = str(self.headers.get("Host") or "")
        headers["X-Forwarded-Proto"] = "http"
        headers["X-Skillbox-Ingress"] = self.server.listener_name
        return headers

    def _write_health(self) -> None:
        payload = self.server.route_store.health_payload()
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        status = 200 if payload.get("ok") else 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _write_text(self, status: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Skillbox ingress reverse proxy.")
    parser.add_argument(
        "--routes-file",
        default=os.environ.get("SKILLBOX_INGRESS_ROUTE_FILE", ""),
        required=not bool(os.environ.get("SKILLBOX_INGRESS_ROUTE_FILE")),
        help="Path to the generated ingress route manifest JSON.",
    )
    parser.add_argument(
        "--public-host",
        default=os.environ.get("SKILLBOX_INGRESS_PUBLIC_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--public-port",
        default=int(os.environ.get("SKILLBOX_INGRESS_PUBLIC_PORT", "8080")),
        type=int,
    )
    parser.add_argument(
        "--private-host",
        default=os.environ.get("SKILLBOX_INGRESS_PRIVATE_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--private-port",
        default=int(os.environ.get("SKILLBOX_INGRESS_PRIVATE_PORT", "9080")),
        type=int,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    route_store = RouteStore(Path(args.routes_file).expanduser())
    servers = [
        IngressServer(
            (args.public_host, args.public_port),
            ProxyHandler,
            listener_name="public",
            route_store=route_store,
        ),
        IngressServer(
            (args.private_host, args.private_port),
            ProxyHandler,
            listener_name="private",
            route_store=route_store,
        ),
    ]

    stop_event = threading.Event()

    def handle_signal(signum: int, _frame: Any) -> None:
        print(f"shutting down ingress proxy on signal {signum}", flush=True)
        stop_event.set()
        for server in servers:
            server.shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    threads: list[threading.Thread] = []
    for server in servers:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
        thread.start()
        threads.append(thread)
        print(
            f"listening on {server.listener_name} {server.server_address[0]}:{server.server_address[1]}",
            flush=True,
        )

    try:
        stop_event.wait()
    finally:
        for server in servers:
            server.server_close()
        for thread in threads:
            thread.join(timeout=2.0)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
