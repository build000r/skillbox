from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from urllib.error import URLError

ENV_MANAGER_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, ".env-manager")
sys.path.insert(0, os.path.abspath(ENV_MANAGER_DIR))

from runtime_manager.distribution.http_security import (  # noqa: E402
    HttpsOnlyError,
    _SecureRedirectHandler,
    require_https,
    secure_opener,
)


class TestHttpSecurity(unittest.TestCase):
    def test_require_https_accepts_https_and_loopback_http(self) -> None:
        for url in (
            "https://dist.example.com/manifest.json",
            "http://127.0.0.1:8000/manifest.json",
            "http://localhost:8000/manifest.json",
            "http://[::1]:8000/manifest.json",
        ):
            with self.subTest(url=url):
                require_https(url)

    def test_require_https_rejects_file_remote_http_and_other_schemes(self) -> None:
        for url in (
            "file:///etc/passwd",
            "http://example.com/manifest.json",
            "ftp://example.com/manifest.json",
            str(Path("/tmp/manifest.json")),
        ):
            with self.subTest(url=url), self.assertRaises(HttpsOnlyError):
                require_https(url)

    def test_redirect_handler_rejects_disallowed_redirect_targets(self) -> None:
        handler = _SecureRedirectHandler()
        for target in (
            "file:///etc/passwd",
            "http://example.com/manifest.json",
            "ftp://example.com/manifest.json",
        ):
            with self.subTest(target=target), self.assertRaises(URLError) as ctx:
                handler.redirect_request(None, None, 302, "Found", {}, target)
            self.assertIn("refusing 302 redirect", str(ctx.exception))

    def test_secure_opener_installs_secure_redirect_handler(self) -> None:
        opener = secure_opener()

        self.assertTrue(any(isinstance(handler, _SecureRedirectHandler) for handler in opener.handlers))
