from __future__ import annotations

import unittest

from scripts.lib import clipboard_fallback as fallback


class ClipboardFallbackTests(unittest.TestCase):
    def test_exact_registered_route_is_the_only_image_action(self) -> None:
        report = fallback.explain_fallback(
            {
                "registered": True,
                "generation_matches": True,
                "pane_matches": True,
                "client_matches": True,
            }
        )
        self.assertEqual(report["image_action"], "registered_route")
        self.assertEqual(report["confidence"], 1.0)

    def test_recognizable_unregistered_ssh_still_refuses_upload(self) -> None:
        report = fallback.explain_fallback(
            {"process_tree_target": "host", "host_alias_known": True}
        )
        self.assertEqual(report["image_action"], "refuse_upload")
        self.assertEqual(report["text_action"], "native_paste")
        self.assertGreater(report["confidence"], 0)

    def test_every_ambiguous_fixture_fails_closed(self) -> None:
        for key in (
            "stale",
            "detached",
            "title_only",
            "multi_hop",
            "conflicting_targets",
            "pane_reused",
        ):
            with self.subTest(key=key):
                report = fallback.explain_fallback({key: True})
                self.assertEqual(report["image_action"], "refuse_upload")
                self.assertEqual(report["state"], "ambiguous")


if __name__ == "__main__":
    unittest.main()
