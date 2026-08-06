from __future__ import annotations

import argparse
import base64
import http.client
import importlib.util
import io
import json
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


ROOT_DIR = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT_DIR / "scripts" / "sbpd.py"
SPEC = importlib.util.spec_from_file_location("sbpd", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
SBPD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SBPD)


class ServerFixture:
    def __init__(self, *, require_auth: bool = False, authenticator=None) -> None:
        self.server = SBPD.ThreadingHTTPServer(("127.0.0.1", 0), SBPD.Handler)
        self.server.require_auth = require_auth
        self.server.authenticator = authenticator
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_address[1],
            timeout=2,
        )
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)


class SbpdHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ServerFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_healthz_and_unknown_path_return_json(self) -> None:
        status, headers, body = self.fixture.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(
            json.loads(body),
            {"ok": True, "service": "sbpd", "version": "v1"},
        )

        status, _headers, body = self.fixture.request("GET", "/unknown")
        self.assertEqual(status, 404)
        self.assertEqual(
            json.loads(body),
            {"ok": False, "error": "not_found", "path": "/unknown"},
        )

    def test_cass_status_and_search_delegate_only_fixed_read_only_commands(self) -> None:
        calls: list[tuple[str, str | None]] = []

        def fake_run(command: str, *, query: str | None = None):
            calls.append((command, query))
            return {"ok": True, "command": command, "query": query}

        with patch.object(SBPD, "run_cass", side_effect=fake_run):
            status, _headers, body = self.fixture.request("GET", "/v1/cass/status")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["command"], "status")

            status, _headers, body = self.fixture.request(
                "GET",
                "/v1/cass/search?q=needle%20with%20spaces",
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["query"], "needle with spaces")

        self.assertEqual(
            calls,
            [("status", None), ("search", "needle with spaces")],
        )

    def test_cass_search_requires_exactly_one_nonempty_query(self) -> None:
        for path in (
            "/v1/cass/search",
            "/v1/cass/search?q=",
            "/v1/cass/search?q=one&q=two",
        ):
            with self.subTest(path=path):
                status, _headers, body = self.fixture.request("GET", path)
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body)["error"], "missing_query")

    def test_skill_pull_streams_tarball_with_identity_header(self) -> None:
        bundle = b"\x1f\x8btest-bundle"
        result = {"tree_sha256": "a" * 64}
        with patch.object(
            SBPD,
            "pull_skill_bundle",
            return_value=(bundle, result),
        ) as pull:
            status, headers, body = self.fixture.request(
                "GET",
                "/v1/skill/pull/sbp",
            )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/gzip")
        self.assertEqual(headers["X-Skill-Tree-Sha256"], "a" * 64)
        self.assertEqual(body, bundle)
        pull.assert_called_once_with("sbp")

    def test_orb_kit_is_public_and_streamed_as_gzip(self) -> None:
        bundle = b"\x1f\x8bdeterministic-kit"
        with patch.object(SBPD, "build_orb_kit", return_value=bundle) as build:
            status, headers, body = self.fixture.request("GET", "/v1/orb-kit")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/gzip")
        self.assertEqual(
            headers["Content-Disposition"],
            'attachment; filename="orb-kit.tar.gz"',
        )
        self.assertEqual(body, bundle)
        build.assert_called_once_with()

    def test_skill_pull_rejects_invalid_name_as_json(self) -> None:
        status, _headers, body = self.fixture.request(
            "GET",
            "/v1/skill/pull/not%2Fa%2Fskill",
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "invalid_skill_name")

    def test_mutating_methods_are_rejected_without_delegation(self) -> None:
        with (
            patch.object(SBPD, "run_cass") as cass,
            patch.object(SBPD, "pull_skill_bundle") as pull,
        ):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                with self.subTest(method=method):
                    status, headers, body = self.fixture.request(
                        method,
                        "/v1/cass/status",
                    )
                    self.assertEqual(status, 405)
                    self.assertEqual(headers["Allow"], "GET")
                    self.assertEqual(json.loads(body)["error"], "method_not_allowed")
        cass.assert_not_called()
        pull.assert_not_called()


class SbpdDelegateTests(unittest.TestCase):
    def test_orb_kit_has_exact_members_and_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_paths = [relative for _name, relative, _mode in SBPD.ORB_KIT_FILES]
            for index, relative in enumerate(source_paths):
                source = root / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(f"fixture-{index}\n", encoding="utf-8")

            first = SBPD.build_orb_kit(root)
            second = SBPD.build_orb_kit(root)

        self.assertEqual(first, second)
        with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as archive:
            self.assertEqual(
                archive.getnames(),
                [
                    "lib/sbp_client.py",
                    "runtime_manager/__init__.py",
                    "runtime_manager/distribution/__init__.py",
                    "runtime_manager/distribution/bundle.py",
                    "orb/join-tailnet.sh",
                    "README.txt",
                ],
            )
            readme = archive.extractfile("README.txt")
            assert readme is not None
            self.assertEqual(len(readme.read().decode("utf-8").splitlines()), 3)
            self.assertEqual(archive.getmember("orb/join-tailnet.sh").mode, 0o755)
            self.assertTrue(all(member.mtime == 0 for member in archive.getmembers()))

    def test_run_cass_uses_canonical_script_and_90_second_inner_timeout(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"status":"ok"}',
            stderr="",
        )
        with patch.object(SBPD.subprocess, "run", return_value=completed) as run:
            payload = SBPD.run_cass("search", query="alpha; rm -rf nope")
        self.assertEqual(payload, {"status": "ok"})
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], [SBPD.sys.executable, str(SBPD.SBP_CASS_SCRIPT), "search"])
        self.assertIn("--timeout-seconds", argv)
        self.assertEqual(argv[argv.index("--timeout-seconds") + 1], "90")
        self.assertEqual(argv[-1], "alpha; rm -rf nope")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_run_cass_maps_timeout_and_nonzero_to_typed_http_errors(self) -> None:
        with patch.object(
            SBPD.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["cass"], 95),
        ):
            with self.assertRaises(SBPD.ServiceError) as timeout:
                SBPD.run_cass("status")
        self.assertEqual(timeout.exception.status, 504)
        self.assertEqual(timeout.exception.payload["error"], "cass_timeout")

        completed = subprocess.CompletedProcess(
            args=[],
            returncode=7,
            stdout='{"status":"error"}',
            stderr="failed",
        )
        with patch.object(SBPD.subprocess, "run", return_value=completed):
            with self.assertRaises(SBPD.ServiceError) as failed:
                SBPD.run_cass("status")
        self.assertEqual(failed.exception.status, 502)
        self.assertEqual(failed.exception.payload["exit_code"], 7)

    def test_pull_skill_bundle_delegates_pull_and_packs_selected_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "sbp"
            source.mkdir()
            (source / "SKILL.md").write_text("# sbp\n", encoding="utf-8")
            (source / "guide.md").write_text("guide\n", encoding="utf-8")
            tree_sha, _entry_sha, _entry_bytes = SBPD.SKILL_PULL._safe_tree_identity(source)
            model = {"model": "fixture"}
            with (
                patch.object(SBPD, "build_runtime_model", return_value=model),
                patch.object(
                    SBPD.SKILL_PULL,
                    "_resolve_internal",
                    return_value=({}, {}, {"sbp": source}),
                ),
                patch.object(
                    SBPD.SKILL_PULL,
                    "pull_host_skill",
                    return_value={"tree_sha256": tree_sha},
                ) as pull,
            ):
                bundle, result = SBPD.pull_skill_bundle("sbp")

        pull.assert_called_once_with(model, "sbp", cwd=SBPD.ROOT_DIR)
        self.assertEqual(result["tree_sha256"], tree_sha)
        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
            self.assertIn("SKILL.md", archive.getnames())
            self.assertIn("guide.md", archive.getnames())
            self.assertIn(".skill-meta/manifest.json", archive.getnames())

    def test_pull_skill_bundle_fails_closed_on_tree_drift(self) -> None:
        model = {"model": "fixture"}
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "sbp"
            source.mkdir()
            (source / "SKILL.md").write_text("# sbp\n", encoding="utf-8")
            with (
                patch.object(SBPD, "build_runtime_model", return_value=model),
                patch.object(
                    SBPD.SKILL_PULL,
                    "_resolve_internal",
                    return_value=({}, {}, {"sbp": source}),
                ),
                patch.object(
                    SBPD.SKILL_PULL,
                    "pull_host_skill",
                    return_value={"tree_sha256": "0" * 64},
                ),
            ):
                with self.assertRaises(SBPD.ServiceError) as drift:
                    SBPD.pull_skill_bundle("sbp")
        self.assertEqual(drift.exception.status, 409)
        self.assertEqual(drift.exception.payload["error_code"], "SKILL_TREE_DRIFT")


class SbpdCliTests(unittest.TestCase):
    def test_bind_defaults_loopback_and_accepts_tailnet_ranges(self) -> None:
        args = SBPD.build_parser().parse_args([])
        self.assertEqual(args.bind, "127.0.0.1")
        self.assertEqual(args.port, 8443)
        self.assertFalse(args.require_auth)
        self.assertTrue(SBPD.build_parser().parse_args(["--require-auth"]).require_auth)
        self.assertEqual(SBPD.bind_address("100.100.1.3"), "100.100.1.3")
        self.assertEqual(
            SBPD.bind_address("fd7a:115c:a1e0::1"),
            "fd7a:115c:a1e0::1",
        )

    def test_bind_rejects_wildcard_and_public_addresses(self) -> None:
        for address in ("0.0.0.0", "::", "8.8.8.8", "localhost"):
            with self.subTest(address=address):
                with self.assertRaises(argparse.ArgumentTypeError):
                    SBPD.bind_address(address)

    def test_ipv6_server_class_uses_ipv6_socket_family(self) -> None:
        self.assertEqual(SBPD.ThreadingHTTPServerV6.address_family, SBPD.socket.AF_INET6)


def _b64url_uint(value: int) -> str:
    size = max(1, (value.bit_length() + 7) // 8)
    return base64.urlsafe_b64encode(value.to_bytes(size, "big")).rstrip(b"=").decode()


def _rsa_jwk(public_key, kid: str) -> dict[str, str]:
    numbers = public_key.public_numbers()
    return {
        "alg": "RS256",
        "e": _b64url_uint(numbers.e),
        "kid": kid,
        "kty": "RSA",
        "n": _b64url_uint(numbers.n),
        "use": "sig",
    }


class _JWKSResponse(io.BytesIO):
    def __init__(self, body: bytes, response_url: str) -> None:
        super().__init__(body)
        self.response_url = response_url

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def geturl(self) -> str:
        return self.response_url


class SequenceJWKSOpener:
    def __init__(
        self,
        *payloads: dict[str, object],
        response_url: str | None = None,
    ) -> None:
        self.payloads = list(payloads)
        self.response_url = response_url
        self.calls = []

    def __call__(self, request, *, timeout: int):
        self.calls.append((request.full_url, timeout))
        index = min(len(self.calls) - 1, len(self.payloads) - 1)
        return _JWKSResponse(
            json.dumps(self.payloads[index]).encode("utf-8"),
            self.response_url or request.full_url,
        )


class SbpdAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        cls.jwk = _rsa_jwk(cls.private_key.public_key(), "primary")
        cls.other_jwk = _rsa_jwk(cls.other_private_key.public_key(), "rotated")

    def token(
        self,
        *,
        private_key=None,
        kid: str = "primary",
        audience: str = SBPD.SBPD_AUDIENCE,
        expires_at: int | None = None,
        subject: str = "project:user:thread",
    ) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "aud": audience,
                "exp": expires_at if expires_at is not None else now + 60,
                "iss": SBPD.AMP_ISSUER,
                "sub": subject,
                "thread_id": "T-test",
            },
            private_key or self.private_key,
            algorithm="RS256",
            headers={"kid": kid},
        )

    def verifier(self, opener=None, **kwargs):
        return SBPD.JWKSVerifier(
            opener=opener or SequenceJWKSOpener({"keys": [self.jwk]}),
            **kwargs,
        )

    def test_valid_token_allows_data_endpoint_and_log_includes_sub(self) -> None:
        fixture = ServerFixture(require_auth=True, authenticator=self.verifier())
        log = io.StringIO()
        try:
            with (
                patch.object(SBPD, "run_cass", return_value={"ok": True}),
                patch.object(SBPD.sys, "stderr", log),
            ):
                status, _headers, body = fixture.request(
                    "GET",
                    "/v1/cass/status",
                    headers={"Authorization": f"Bearer {self.token()}"},
                )
        finally:
            fixture.close()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertIn("sub=project:user:thread", log.getvalue())

    def test_missing_expired_wrong_audience_and_unknown_kid_return_json_401(self) -> None:
        fixture = ServerFixture(require_auth=True, authenticator=self.verifier())
        now = int(time.time())
        unsigned = jwt.encode(
            {
                "aud": SBPD.SBPD_AUDIENCE,
                "exp": now + 60,
                "iss": SBPD.AMP_ISSUER,
            },
            key="",
            algorithm="none",
            headers={"kid": "primary"},
        )
        hs256 = jwt.encode(
            {
                "aud": SBPD.SBPD_AUDIENCE,
                "exp": now + 60,
                "iss": SBPD.AMP_ISSUER,
            },
            key="not-the-rsa-key-" * 4,
            algorithm="HS256",
            headers={"kid": "primary"},
        )
        missing_audience = jwt.encode(
            {
                "exp": now + 60,
                "iss": SBPD.AMP_ISSUER,
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": "primary"},
        )
        missing_expiry = jwt.encode(
            {
                "aud": SBPD.SBPD_AUDIENCE,
                "iss": SBPD.AMP_ISSUER,
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": "primary"},
        )
        cases = {
            "missing": None,
            "alg-none": unsigned,
            "hs256": hs256,
            "missing-aud": missing_audience,
            "missing-exp": missing_expiry,
            "expired": self.token(expires_at=int(time.time()) - 60),
            "wrong-aud": self.token(audience="some-other-service"),
            "unknown-kid": self.token(kid="unknown"),
        }
        try:
            with patch.object(SBPD, "run_cass") as cass:
                for name, token in cases.items():
                    with self.subTest(name=name):
                        headers = {"Authorization": f"Bearer {token}"} if token else {}
                        status, response_headers, body = fixture.request(
                            "GET",
                            "/v1/cass/status",
                            headers=headers,
                        )
                        self.assertEqual(status, 401)
                        self.assertEqual(response_headers["Content-Type"], "application/json; charset=utf-8")
                        self.assertEqual(
                            json.loads(body),
                            {
                                "error": "unauthorized",
                                "message": "Authentication required",
                                "ok": False,
                            },
                        )
                        self.assertEqual(
                            response_headers["WWW-Authenticate"],
                            'Bearer realm="sbpd"',
                        )
            cass.assert_not_called()
        finally:
            fixture.close()

    def test_health_and_orb_kit_are_exempt_when_auth_is_required(self) -> None:
        fixture = ServerFixture(require_auth=True, authenticator=self.verifier())
        try:
            status, _headers, _body = fixture.request("GET", "/healthz")
            self.assertEqual(status, 200)
            with patch.object(SBPD, "build_orb_kit", return_value=b"kit"):
                status, _headers, body = fixture.request("GET", "/v1/orb-kit")
            self.assertEqual(status, 200)
            self.assertEqual(body, b"kit")
        finally:
            fixture.close()

    def test_skill_endpoint_requires_auth_before_delegation(self) -> None:
        fixture = ServerFixture(require_auth=True, authenticator=self.verifier())
        try:
            with patch.object(SBPD, "pull_skill_bundle") as pull:
                status, headers, body = fixture.request(
                    "GET",
                    "/v1/skill/pull/sbp",
                )
            self.assertEqual(status, 401)
            self.assertEqual(headers["WWW-Authenticate"], 'Bearer realm="sbpd"')
            self.assertEqual(json.loads(body)["error"], "unauthorized")
            pull.assert_not_called()
        finally:
            fixture.close()

    def test_jwks_cache_uses_ttl_and_refetches_on_kid_miss(self) -> None:
        now = [100.0]
        opener = SequenceJWKSOpener(
            {"keys": [self.jwk]},
            {"keys": [self.jwk, self.other_jwk]},
            {"keys": [self.jwk, self.other_jwk]},
        )
        verifier = self.verifier(
            opener=opener,
            cache_ttl_seconds=10,
            clock=lambda: now[0],
        )

        verifier.verify(self.token())
        verifier.verify(self.token())
        self.assertEqual(len(opener.calls), 1)

        verifier.verify(
            self.token(
                private_key=self.other_private_key,
                kid="rotated",
            )
        )
        self.assertEqual(len(opener.calls), 2)

        for kid in ("unknown-1", "unknown-2", "unknown-3"):
            with self.assertRaises(SBPD.AuthenticationError):
                verifier.verify(self.token(kid=kid))
        self.assertEqual(len(opener.calls), 2)

        now[0] = 111.0
        verifier.verify(self.token())
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(
            opener.calls[0],
            (SBPD.AMP_JWKS_URL, SBPD.JWKS_TIMEOUT_SECONDS),
        )
        with self.assertRaisesRegex(SBPD.AuthenticationError, "must use HTTPS"):
            SBPD.JWKSVerifier(jwks_url="http://attacker.invalid/jwks.json")
        redirected = self.verifier(
            opener=SequenceJWKSOpener(
                {"keys": [self.jwk]},
                response_url="http://attacker.invalid/jwks.json",
            )
        )
        with self.assertRaises(SBPD.AuthenticationError):
            redirected.verify(self.token())

        poisoning_opener = SequenceJWKSOpener(
            {"keys": [self.jwk]},
            {
                "keys": [
                    {
                        "alg": "HS256",
                        "kid": "primary",
                        "kty": "oct",
                        "use": "sig",
                    }
                ]
            },
        )
        poisoning_verifier = self.verifier(
            opener=poisoning_opener,
            clock=lambda: 200.0,
        )
        poisoning_verifier.verify(self.token())
        with self.assertRaises(SBPD.AuthenticationError):
            poisoning_verifier.verify(self.token(kid="poison"))
        poisoning_verifier.verify(self.token())
        self.assertEqual(len(poisoning_opener.calls), 2)


if __name__ == "__main__":
    unittest.main()
