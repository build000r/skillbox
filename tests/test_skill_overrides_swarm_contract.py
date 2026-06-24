"""Regression checks for the skill-overrides swarm observer contract."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
CONTRACT = ROOT_DIR / "docs" / "skill-overrides-swarm-contract.md"
WORKGRAPH = ROOT_DIR / "plans" / "skill-overrides-swarm" / "WORKGRAPH.md"


class SkillOverridesSwarmContractTests(unittest.TestCase):
    maxDiff = None

    def test_contract_documents_required_observer_controls(self) -> None:
        text = CONTRACT.read_text(encoding="utf-8")

        required = [
            "tmux target",
            "Beads actor",
            "Expected Artifacts",
            ".skillbox/skill-overrides.yaml",
            "tests/test_skill_overrides.py",
            "docs/SBP_OUTPUT_SCHEMAS.md",
            "br close <issue-id>",
            "20-30 minutes",
            "No-Nudge Conditions",
            "sbp send-later",
            "--when-waiting",
            "verify the target pane again",
        ]
        missing = [item for item in required if item not in text]
        self.assertEqual([], missing)

    def test_marching_orders_reference_contract_and_identity_gate(self) -> None:
        text = WORKGRAPH.read_text(encoding="utf-8")

        self.assertIn("../../docs/skill-overrides-swarm-contract.md", text)
        self.assertIn("live tmux target and Beads actor", text)
        self.assertIn("sbp send-later schedule --when-waiting", text)
        self.assertIn("20-30 minutes", text)


if __name__ == "__main__":
    unittest.main()
