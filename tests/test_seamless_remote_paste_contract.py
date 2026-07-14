"""Contract tests for the normative seamless remote paste specification."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
CONTRACT = ROOT_DIR / "docs" / "seamless-remote-paste.md"


class SeamlessRemotePasteContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = CONTRACT.read_text(encoding="utf-8")

    def test_operator_journey_forbids_helper_friction(self) -> None:
        normalized = " ".join(self.text.split())
        for phrase in (
            "one explicit `Cmd+V` or `Ctrl+V` keypress",
            "never sends Enter",
            "never replaces the Mac clipboard",
            "no helper command, host selector, second paste",
        ):
            self.assertIn(phrase, normalized)

    def test_clipboard_behavior_covers_required_states(self) -> None:
        for state in (
            "Plain or rich text",
            "Multiline or shell metacharacters",
            "PNG or TIFF image",
            "Supported file URL",
            "Empty clipboard",
            "Unsupported media",
            "Oversized or corrupt media",
            "Offline target",
            "Focus changes in flight",
            "Clipboard changes in flight",
        ):
            self.assertIn(f"| {state} |", self.text)

    def test_surface_matrix_covers_core_routes(self) -> None:
        for surface in (
            "Direct d3",
            "d2 to devbox-N",
            "Nested local + remote tmux",
            "Direct remote tmux",
            "d3 over mosh",
            "Codex native bridge",
            "Codex desktop SSH composer",
            "Local TUI + remote app-server",
        ):
            self.assertIn(f"| {surface} |", self.text)

    def test_latency_budget_has_numeric_p50_p95_and_timeout(self) -> None:
        self.assertRegex(self.text, r"Warm image paste p50 \| at most 500 ms")
        self.assertRegex(self.text, r"Warm image paste p95 \| at most 1\.5 s")
        self.assertRegex(self.text, r"Offline/auth timeout \| at most 3 s")

    def test_audience_outcomes_are_explicit(self) -> None:
        for audience in ("Operator", "Agent", "Maintainer"):
            self.assertIn(f"| {audience} |", self.text)

    def test_threat_ids_are_contiguous_and_have_table_rows(self) -> None:
        ids = [
            int(value)
            for value in re.findall(r"^\| SRP-T(\d{2}) \|", self.text, re.MULTILINE)
        ]
        self.assertEqual(ids, list(range(1, 19)))
        threat_table = re.search(
            r"## Threat model and test ownership\n\n(?P<table>.+?)\n\nEvery implementation",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(threat_table)
        for line in threat_table.group("table").splitlines()[2:]:  # type: ignore[union-attr]
            if not line.startswith("| SRP-"):
                continue
            self.assertEqual(line.count("|"), 5, msg=line)

    def test_security_invariants_cover_required_boundaries(self) -> None:
        for phrase in (
            "explicit paste gesture",
            "`0.0.0.0`",
            "Window titles alone are never routing authority",
            "constant time",
            "without following symlinks",
            "Replay cannot create a second injection",
            "never enter logs",
            "Multi-user remote hosts are unsupported",
        ):
            self.assertIn(phrase, self.text)

    def test_clipboard_bootstrap_links_normative_contract(self) -> None:
        bootstrap = (ROOT_DIR / "docs" / "clipboard-bootstrap.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "[`docs/seamless-remote-paste.md`](seamless-remote-paste.md)", bootstrap
        )


if __name__ == "__main__":
    unittest.main()
