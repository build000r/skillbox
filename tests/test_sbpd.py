from __future__ import annotations

import argparse
import http.client
import importlib.util
import io
import json
import subprocess
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT_DIR / "scripts" / "sbpd.py"
SPEC = importlib.util.spec_from_file_location("sbpd", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
SBPD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SBPD)


class ServerFixture:
    def __init__(self) -> None:
        self.server = SBPD.ThreadingHTTPServer(("127.0.0.1", 0), SBPD.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(
        self,
        method: str,
        path: str,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_address[1],
            timeout=2,
        )
        try:
            connection.request(method, path)
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


if __name__ == "__main__":
    unittest.main()
