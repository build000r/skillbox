from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CLIENT_PATH = ROOT / "scripts" / "lib" / "sbp_client.py"
SBP_PATH = ROOT / "scripts" / "sbp"

SPEC = importlib.util.spec_from_file_location("sbp_client", CLIENT_PATH)
assert SPEC is not None and SPEC.loader is not None
SBP_CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SBP_CLIENT)


class Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body


class SbpClientUnitTests(unittest.TestCase):
    def test_search_encodes_query_and_prints_envelope_verbatim(self) -> None:
        body = b'{ "status": "ok", "result": [1] }\n'
        output = io.BytesIO()
        opener = mock.Mock(return_value=Response(body))

        result = SBP_CLIENT.run_remote_cass(
            "http://box.test:8443/",
            ["search", "exact phrase", "--json"],
            opener=opener,
            stdout=output,
        )

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), body)
        request = opener.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://box.test:8443/v1/cass/search?q=exact+phrase",
        )
        self.assertEqual(opener.call_args.kwargs["timeout"], 90.0)

    def test_status_maps_to_read_only_endpoint(self) -> None:
        opener = mock.Mock(return_value=Response(b"{}"))

        result = SBP_CLIENT.run_remote_cass(
            "http://box.test:8443",
            ["--json", "status"],
            opener=opener,
            stdout=io.BytesIO(),
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            opener.call_args.args[0].full_url,
            "http://box.test:8443/v1/cass/status",
        )

    def test_unsupported_remote_verb_fails_without_http_request(self) -> None:
        opener = mock.Mock()
        errors = io.StringIO()

        result = SBP_CLIENT.run_remote_cass(
            "http://box.test:8443",
            ["rebuild"],
            opener=opener,
            stdout=io.BytesIO(),
            stderr=errors,
        )

        self.assertEqual(result, 2)
        self.assertIn("does not support 'rebuild'", errors.getvalue())
        opener.assert_not_called()


class SbpDispatchTests(unittest.TestCase):
    def test_sbp_remote_dispatches_search_over_http(self) -> None:
        body = b'{"status":"ok","result":{"hits":[{"id":"box"}]}}'
        seen_paths: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen_paths.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                del args

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        env = os.environ.copy()
        env["SBP_REMOTE"] = f"http://127.0.0.1:{server.server_port}"
        result = subprocess.run(
            [str(SBP_PATH), "cass", "search", "needle with spaces"],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, body)
        self.assertEqual(
            seen_paths,
            ["/v1/cass/search?q=needle+with+spaces"],
        )

    def test_sbp_without_remote_uses_legacy_cass_wrapper_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_root = Path(temp_dir)
            (config_root / "clients").mkdir()
            scripts_dir = config_root / "scripts"
            scripts_dir.mkdir()
            wrapper = scripts_dir / "sbp_cass.py"
            wrapper.write_text(
                "import json, sys\n"
                "sys.stdout.write(json.dumps({'argv': sys.argv[1:]}, separators=(',', ':')))\n",
                encoding="utf-8",
            )
            expected = b'{"argv":["search","local query","--json"]}'
            env = os.environ.copy()
            env.pop("SBP_REMOTE", None)
            env["SKILLBOX_CONFIG_ROOT"] = str(config_root)

            result = subprocess.run(
                [str(SBP_PATH), "cass", "search", "local query", "--json"],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, expected)


if __name__ == "__main__":
    unittest.main()
