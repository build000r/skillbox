#!/usr/bin/env python3
"""Read-only HTTP bridge for host Cass search and skill pull."""

from __future__ import annotations

import argparse
import gzip
import ipaddress
import io
import json
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import request as urllib_request
from urllib.parse import parse_qs, unquote, urlsplit

import jwt


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
AMP_JWKS_URL = "https://ampcode.com/api/workload-identity/jwks.json"
AMP_ISSUER = "https://ampcode.com/api/workload-identity"
SBPD_AUDIENCE = "sbpd"
JWKS_CACHE_TTL_SECONDS = 300
JWKS_TIMEOUT_SECONDS = 10
JWKS_KID_REFRESH_COOLDOWN_SECONDS = 30
PROTECTED_PATH_PREFIXES = ("/v1/cass/", "/v1/skill/")
ORB_KIT_FILES = (
    ("lib/sbp_client.py", Path("scripts/lib/sbp_client.py"), 0o644),
    ("runtime_manager/__init__.py", Path(".env-manager/runtime_manager/__init__.py"), 0o644),
    (
        "runtime_manager/distribution/__init__.py",
        Path(".env-manager/runtime_manager/distribution/__init__.py"),
        0o644,
    ),
    (
        "runtime_manager/distribution/bundle.py",
        Path(".env-manager/runtime_manager/distribution/bundle.py"),
        0o644,
    ),
    ("orb/join-tailnet.sh", Path("scripts/orb/join-tailnet.sh"), 0o755),
)
ORB_KIT_README = (
    "Run orb/join-tailnet.sh to join the Skillbox tailnet.\n"
    'Export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}" and '
    'SBP_REMOTE="http://<skillbox-tailnet-ip>:8443".\n'
    "Run python3 lib/sbp_client.py cass status --json.\n"
).encode("utf-8")


class ServiceError(RuntimeError):
    """HTTP-safe typed service failure."""

    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("message") or payload.get("error") or "service error"))
        self.status = status
        self.payload = payload


class AuthenticationError(RuntimeError):
    """A bearer token cannot be authenticated without exposing internals."""


def _require_https_jwks_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise AuthenticationError("workload-identity JWKS URL must use HTTPS")


class JWKSVerifier:
    """Verify Amp workload-identity tokens against a bounded JWKS cache."""

    def __init__(
        self,
        *,
        jwks_url: str = AMP_JWKS_URL,
        audience: str = SBPD_AUDIENCE,
        issuer: str = AMP_ISSUER,
        cache_ttl_seconds: int = JWKS_CACHE_TTL_SECONDS,
        timeout_seconds: int = JWKS_TIMEOUT_SECONDS,
        kid_refresh_cooldown_seconds: int = JWKS_KID_REFRESH_COOLDOWN_SECONDS,
        opener: Any = urllib_request.urlopen,
        clock: Any = time.monotonic,
    ) -> None:
        _require_https_jwks_url(jwks_url)
        self.jwks_url = jwks_url
        self.audience = audience
        self.issuer = issuer
        self.cache_ttl_seconds = cache_ttl_seconds
        self.timeout_seconds = timeout_seconds
        self.kid_refresh_cooldown_seconds = kid_refresh_cooldown_seconds
        self.opener = opener
        self.clock = clock
        self._keys: dict[str, dict[str, Any]] = {}
        self._expires_at = 0.0
        self._next_kid_refresh_at = 0.0
        self._lock = threading.Lock()

    def _fetch_keys(self) -> dict[str, dict[str, Any]]:
        request = urllib_request.Request(
            self.jwks_url,
            headers={"Accept": "application/json"},
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                response_url = (
                    response.geturl()
                    if callable(getattr(response, "geturl", None))
                    else self.jwks_url
                )
                if not isinstance(response_url, str):
                    raise AuthenticationError(
                        "workload-identity JWKS response URL is invalid"
                    )
                _require_https_jwks_url(response_url)
                payload = json.load(response)
        except Exception as exc:
            raise AuthenticationError("unable to load workload-identity keys") from exc

        raw_keys = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(raw_keys, list):
            raise AuthenticationError("workload-identity JWKS has no keys list")
        keys = {
            key["kid"]: key
            for key in raw_keys
            if isinstance(key, dict)
            and isinstance(key.get("kid"), str)
            and key.get("kid")
            and key.get("kty") == "RSA"
            and key.get("alg") == "RS256"
            and key.get("use") in {None, "sig"}
        }
        if not keys:
            raise AuthenticationError("workload-identity JWKS has no RS256 signing keys")
        return keys

    def _cached_keys(self, *, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
        with self._lock:
            now = self.clock()
            if force_refresh:
                if now < self._next_kid_refresh_at:
                    return dict(self._keys)
                self._next_kid_refresh_at = (
                    now + self.kid_refresh_cooldown_seconds
                )
            if force_refresh or not self._keys or now >= self._expires_at:
                self._keys = self._fetch_keys()
                self._expires_at = now + self.cache_ttl_seconds
            return dict(self._keys)

    def _jwk_for_kid(self, kid: str) -> dict[str, Any]:
        key = self._cached_keys().get(kid)
        if key is None:
            key = self._cached_keys(force_refresh=True).get(kid)
        if key is None:
            raise AuthenticationError("token kid is not present in workload-identity JWKS")
        return key

    def verify(self, token: str) -> dict[str, Any]:
        """Return verified claims for one RS256 Amp workload-identity JWT."""
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                raise AuthenticationError("token algorithm must be RS256")
            kid = header.get("kid")
            if not isinstance(kid, str) or not kid:
                raise AuthenticationError("token has no kid")
            public_key = jwt.algorithms.RSAAlgorithm.from_jwk(
                json.dumps(self._jwk_for_kid(kid))
            )
            claims = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["aud", "exp"]},
            )
        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError("invalid workload-identity token") from exc
        if not isinstance(claims, dict):
            raise AuthenticationError("invalid workload-identity claims")
        return claims


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


def build_orb_kit(root_dir: Path = ROOT_DIR) -> bytes:
    """Build the deterministic bootstrap kit from current repo files."""
    members: list[tuple[str, bytes, int]] = []
    for archive_name, relative_path, mode in ORB_KIT_FILES:
        source = root_dir / relative_path
        try:
            body = source.read_bytes()
        except OSError as exc:
            raise ServiceError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "ok": False,
                    "error": "orb_kit_unavailable",
                    "message": f"Orb kit source is unavailable: {relative_path.as_posix()}",
                },
            ) from exc
        members.append((archive_name, body, mode))
    members.append(("README.txt", ORB_KIT_README, 0o644))

    output = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=output,
        compresslevel=9,
        mtime=0,
    ) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
            for archive_name, body, mode in members:
                info = tarfile.TarInfo(archive_name)
                info.size = len(body)
                info.mode = mode
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                archive.addfile(info, io.BytesIO(body))
    return output.getvalue()


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

    def _send_json(
        self,
        status: int,
        payload: Any,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
            headers=headers,
        )

    def _authenticate(self, path: str) -> bool:
        self.auth_sub = None
        require_auth = bool(getattr(self.server, "require_auth", False))
        if not require_auth or not path.startswith(PROTECTED_PATH_PREFIXES):
            return True

        authorization = self.headers.get_all("Authorization", [])
        if len(authorization) == 1:
            scheme, separator, token = authorization[0].partition(" ")
            if scheme.lower() == "bearer" and separator and token.strip():
                verifier = getattr(self.server, "authenticator", None)
                try:
                    if verifier is None:
                        raise AuthenticationError("authentication is not configured")
                    claims = verifier.verify(token.strip())
                    subject = claims.get("sub")
                    self.auth_sub = subject if isinstance(subject, str) else None
                    return True
                except AuthenticationError:
                    pass

        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            {
                "ok": False,
                "error": "unauthorized",
                "message": "Authentication required",
            },
            headers={"WWW-Authenticate": 'Bearer realm="sbpd"'},
        )
        return False

    def do_GET(self) -> None:  # noqa: N802
        request = urlsplit(self.path)
        try:
            if not self._authenticate(request.path):
                return

            if request.path == "/healthz":
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "service": "sbpd", "version": "v1"},
                )
                return

            if request.path == "/v1/orb-kit":
                self._send_bytes(
                    HTTPStatus.OK,
                    build_orb_kit(),
                    "application/gzip",
                    headers={
                        "Content-Disposition": 'attachment; filename="orb-kit.tar.gz"',
                    },
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
        request = urlsplit(self.path)
        if not self._authenticate(request.path):
            return
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
        subject = getattr(self, "auth_sub", None) or "-"
        subject = json.dumps(subject, ensure_ascii=True)[1:-1]
        sys.stderr.write(
            "sbpd: %s sub=%s - %s\n"
            % (self.client_address[0], subject, fmt % args)
        )


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
    parser.add_argument(
        "--require-auth",
        action="store_true",
        help="Require Amp workload-identity bearer JWTs for data endpoints",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server_class = (
        ThreadingHTTPServerV6
        if ipaddress.ip_address(args.bind).version == 6
        else ThreadingHTTPServer
    )
    server = server_class((args.bind, args.port), Handler)
    server.require_auth = args.require_auth
    server.authenticator = JWKSVerifier() if args.require_auth else None
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
