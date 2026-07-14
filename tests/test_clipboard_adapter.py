from __future__ import annotations

import unittest

from scripts.lib import clipboard_adapter as adapter


class ClipboardAdapterTests(unittest.TestCase):
    def test_codex_remote_path_becomes_visible_attachment(self) -> None:
        result = adapter.choose_adapter(
            agent="codex",
            agent_version="codex-cli 0.144.4",
            input_kind="remote_image_path",
            route_ready=True,
        )
        self.assertEqual(result.state, "ready")
        self.assertEqual(result.strategy, "path_paste_attachment")
        self.assertTrue(result.visible_attachment)

    def test_unknown_codex_version_degrades_to_explicit_reference(self) -> None:
        result = adapter.choose_adapter(
            agent="codex",
            agent_version=None,
            input_kind="remote_image_path",
            route_ready=True,
        )
        self.assertEqual(result.state, "degraded")
        self.assertEqual(result.strategy, "text_reference")

    def test_unproven_old_codex_version_never_claims_visible_attachment(self) -> None:
        result = adapter.choose_adapter(
            agent="codex",
            agent_version="codex-cli 0.143.9",
            input_kind="remote_image_path",
            route_ready=True,
        )
        self.assertEqual(result.state, "degraded")
        self.assertEqual(result.strategy, "text_reference")
        self.assertFalse(result.visible_attachment)

    def test_native_local_and_generic_fallback_contracts(self) -> None:
        native = adapter.choose_adapter(
            agent="codex",
            agent_version="0.144.4",
            input_kind="local_image_path",
            route_ready=True,
            native_clipboard=True,
        )
        generic = adapter.choose_adapter(
            agent="generic",
            agent_version="1.0.0",
            input_kind="remote_image_path",
            route_ready=True,
        )
        self.assertEqual(native.strategy, "native_attachment")
        self.assertEqual(generic.strategy, "path_text_reference")
        self.assertFalse(generic.visible_attachment)

    def test_unsupported_input_and_stale_route_do_not_claim_attachment(self) -> None:
        unsupported = adapter.choose_adapter(
            agent="codex", agent_version="0.144.4", input_kind="video", route_ready=True
        )
        stale = adapter.choose_adapter(
            agent="codex",
            agent_version="0.144.4",
            input_kind="remote_image_path",
            route_ready=False,
        )
        self.assertEqual(unsupported.state, "unsupported")
        self.assertFalse(stale.visible_attachment)


if __name__ == "__main__":
    unittest.main()
