#!/usr/bin/env python3
"""Read-only HTTP bridge for host Cass search and skill pull."""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import subprocess
import sys
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import skill_pull as SKILL_PULL  # noqa: E402
from runtime_manager.distribution.bundle import pack_skill_bundle  # noqa: E402
from lib.runtime_model import build_runtime_model  # noqa: E402


DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8443
CASS_TIMEOUT_SECONDS = 90
CASS_PROCESS_TIMEOUT_SECONDS = CASS_TIMEOUT_SECONDS + 5
SBP_CASS_SCRIPT = Path(
    "/srv/skillbox/repos/skillbox-config/scripts/sbp_cass.py"
)
TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


class ServiceError(RuntimeError):
    """HTTP-safe typed service failure."""

    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("message") or payload.get("error") or "service error"))
        self.status = status
        self.payload = payload


def bind_address(value: str) -> str:
    """Accept only an IP on loopback or the Tailscale address ranges."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--bind must be a literal IP address") from exc
    allowed = address.is_loopback or address in TAILSCALE_V4 or address in TAILSCALE_V6
    if not allowed:
        raise argparse.ArgumentTypeError(
            "--bind must be loopback or a Tailnet IP; wildcard/public binds are forbidden"
        )
    return str(address)


def _json_from_process(stdout: str) -> Any:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ServiceError(
            HTTPStatus.BAD_GATEWAY,
            {
                "ok": False,
                "error": "cass_invalid_response",
                "message": "sbp_cass.py did not return valid JSON",
            },
        ) from exc


def run_cass(command: str, *, query: str | None = None) -> Any:
    """Run one fixed read-only Cass command through the canonical front door."""
    if command == "status":
        argv = [
            sys.executable,
            str(SBP_CASS_SCRIPT),
            "status",
            "--json",
            "--timeout-seconds",
            str(CASS_TIMEOUT_SECONDS),
        ]
    elif command == "search" and query is not None:
        argv = [
            sys.executable,
            str(SBP_CASS_SCRIPT),
            "search",
            "--json",
            "--timeout-seconds",
            str(CASS_TIMEOUT_SECONDS),
            query,
        ]
    else:
        raise ValueError(f"unsupported Cass command: {command}")

    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=CASS_PROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ServiceError(
            HTTPStatus.GATEWAY_TIMEOUT,
            {
                "ok": False,
                "error": "cass_timeout",
                "message": f"Cass request exceeded {CASS_TIMEOUT_SECONDS}s",
            },
        ) from exc
    except OSError as exc:
        raise ServiceError(
            HTTPStatus.BAD_GATEWAY,
            {
                "ok": False,
                "error": "cass_unavailable",
                "message": "Unable to execute sbp_cass.py",
            },
        ) from exc

    payload = _json_from_process(completed.stdout)
    if completed.returncode != 0:
        raise ServiceError(
            HTTPStatus.BAD_GATEWAY,
            {
                "ok": False,
                "error": "cass_failed",
                "exit_code": completed.returncode,
                "result": payload,
                "stderr": completed.stderr.strip()[-2000:],
            },
        )
    return payload


def pull_skill_bundle(name: str) -> tuple[bytes, dict[str, Any]]:
    """Resolve a host skill, recheck it, and return a deterministic bundle."""
    if not SKILL_PULL.SKILL_NAME_RE.fullmatch(name):
        raise ServiceError(
            HTTPStatus.BAD_REQUEST,
            {
                "ok": False,
                "error": "invalid_skill_name",
                "message": "Skill name must match the host skill naming contract",
            },
        )

    model = build_runtime_model(ROOT_DIR)
    try:
        _request, _receipt, sources = SKILL_PULL._resolve_internal(
            model,
            cwd=ROOT_DIR,
            explicit_skills=[name],
        )
        result = SKILL_PULL.pull_host_skill(model, name, cwd=ROOT_DIR)
        source = sources[name]
        observed_tree, _entry_sha, _entry_bytes = SKILL_PULL._safe_tree_identity(source)
        if observed_tree != result["tree_sha256"]:
            raise SKILL_PULL.SkillPullError(
                "SKILL_TREE_DRIFT",
                "Skill source changed before bundle creation.",
            )
        with tempfile.TemporaryDirectory(prefix="sbpd-skill-") as temporary:
            bundle_path = pack_skill_bundle(
                source,
                1,
                name=name,
                output_dir=Path(temporary),
            )
            return bundle_path.read_bytes(), result
    except SKILL_PULL.SkillPullError as exc:
        status = (
            HTTPStatus.NOT_FOUND
            if exc.error_code in {"SKILL_NOT_ADMITTED", "SKILL_SOURCE_MISSING"}
            else HTTPStatus.CONFLICT
            if exc.error_code == "SKILL_TREE_DRIFT"
            else HTTPStatus.UNPROCESSABLE_ENTITY
        )
        raise ServiceError(status, exc.envelope()) from exc


class Handler(BaseHTTPRequestHandler):
    """Exact GET-only sbpd v1 route table."""

    server_version = "sbpd/1"

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        request = urlsplit(self.path)
        try:
            if request.path == "/healthz":
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "service": "sbpd", "version": "v1"},
                )
                return

            if request.path == "/v1/cass/status":
                self._send_json(HTTPStatus.OK, run_cass("status"))
                return

            if request.path == "/v1/cass/search":
                query_values = parse_qs(
                    request.query,
                    keep_blank_values=True,
                ).get("q", [])
                query = query_values[0].strip() if len(query_values) == 1 else ""
                if not query:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "ok": False,
                            "error": "missing_query",
                            "message": "Exactly one non-empty q parameter is required",
                        },
                    )
                    return
                self._send_json(HTTPStatus.OK, run_cass("search", query=query))
                return

            prefix = "/v1/skill/pull/"
            if request.path.startswith(prefix):
                name = unquote(request.path[len(prefix) :])
                bundle, result = pull_skill_bundle(name)
                self._send_bytes(
                    HTTPStatus.OK,
                    bundle,
                    "application/gzip",
                    headers={
                        "Content-Disposition": (
                            f'attachment; filename="{name}-v1.skillbundle.tar.gz"'
                        ),
                        "X-Skill-Tree-Sha256": str(result["tree_sha256"]),
                    },
                )
                return

            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": "not_found", "path": request.path},
            )
        except ServiceError as exc:
            self._send_json(exc.status, exc.payload)
        except Exception:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": "internal_error",
                    "message": "sbpd request failed",
                },
            )

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        body = b'{"error":"method_not_allowed","ok":false}'
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("sbpd: %s - %s\n" % (self.client_address[0], fmt % args))


class ThreadingHTTPServerV6(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bind",
        type=bind_address,
        default=DEFAULT_BIND,
        help="Loopback or literal Tailnet IP (default: 127.0.0.1)",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server_class = (
        ThreadingHTTPServerV6
        if ipaddress.ip_address(args.bind).version == 6
        else ThreadingHTTPServer
    )
    server = server_class((args.bind, args.port), Handler)
    print(f"sbpd serving http://{args.bind}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
