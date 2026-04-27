from __future__ import annotations

import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
SWIMMERS_SCRIPT = ROOT_DIR / "scripts" / "05-swimmers.sh"


class SwimmersScriptContractTests(unittest.TestCase):
    def test_start_server_exports_swimmers_bind_from_publish_host(self) -> None:
        script_text = SWIMMERS_SCRIPT.read_text(encoding="utf-8")
        start_idx = script_text.index("start_server() {")
        stop_idx = script_text.index("  stop_server() {", start_idx)
        start_server_block = script_text[start_idx:stop_idx]

        self.assertIn('SWIMMERS_BIND="${swimmers_publish_host}"', start_server_block)

    def test_non_loopback_publish_still_requires_token_auth(self) -> None:
        script_text = SWIMMERS_SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            'if ! is_loopback_host "${swimmers_publish_host}" && [[ "${swimmers_auth_mode}" != "token" ]]; then',
            script_text,
        )
        self.assertIn(
            "Refusing to expose swimmers on %s without token auth. Set SKILLBOX_SWIMMERS_AUTH_MODE=token and "
            "SKILLBOX_SWIMMERS_AUTH_TOKEN.",
            script_text,
        )


if __name__ == "__main__":
    unittest.main()
