from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import agent_search as SEARCH  # noqa: E402
from runtime_manager.text_renderers import search_brain_text_lines  # noqa: E402


def _assert_elapsed_meta(testcase: unittest.TestCase, payload: dict[str, object]) -> None:
    meta = payload.get("meta")
    testcase.assertIsInstance(meta, dict)
    elapsed = meta.get("elapsed_ms") if isinstance(meta, dict) else None
    testcase.assertIsInstance(elapsed, (int, float))
    testcase.assertNotIsInstance(elapsed, bool)
    testcase.assertGreaterEqual(float(elapsed), 0.0)


def _assert_error_envelope(testcase: unittest.TestCase, payload: dict[str, object], code: str) -> None:
    testcase.assertIs(payload["ok"], False)
    error = payload.get("error")
    testcase.assertIsInstance(error, dict)
    testcase.assertEqual(error["code"], code)
    testcase.assertEqual(error["type"], code)
    testcase.assertEqual(payload["error_code"], code)
    testcase.assertIn("deprecation", payload)


def _graph() -> dict[str, object]:
    return {
        "nodes": [
            {
                "id": "service:api",
                "kind": "service",
                "label": "API service",
                "attrs": {"status": "running", "summary": "runtime graph API"},
            },
            {
                "id": "mcp_tool:skillbox_next",
                "kind": "mcp_tool",
                "label": "skillbox_next",
                "attrs": {"summary": "next action MCP tool"},
            },
        ],
        "edges": [],
    }


def _adapters() -> dict[str, object]:
    return {
        "br_ready": {
            "ok": True,
            "payload": [
                {
                    "id": "skillbox-agent-ops-search-xeu",
                    "title": "Implement local agent search",
                    "status": "open",
                    "priority": 2,
                }
            ],
        }
    }


def _evidence() -> dict[str, object]:
    return {
        "overall": "yellow",
        "blocked_conditions": ["mcp: 1 invalid config"],
        "sections": {"mcp": {"invalid_configs": 1, "next_actions": ["mcp-audit --format json"]}},
    }


class AgentSearchTests(unittest.TestCase):
    def test_search_returns_grouped_ranked_hits_across_sources(self) -> None:
        payload = SEARCH.search_payload(
            "graph",
            graph=_graph(),
            adapters=_adapters(),
            evidence=_evidence(),
            docs={"README.md": "Skillbox runtime graph and agent command guide."},
        )

        self.assertTrue(payload["ok"])
        sources = {group["source"] for group in payload["groups"]}
        self.assertIn("registry", sources)
        self.assertIn("graph", sources)
        self.assertIn("docs", sources)
        self.assertGreaterEqual(payload["hits"][0]["score"], payload["hits"][-1]["score"])
        for hit in payload["hits"]:
            self.assertIn("kind", hit)
            self.assertIn("source", hit)
            self.assertIn("score", hit)
            self.assertIn("snippet", hit)
            self.assertIn("next_action", hit)
            self.assertNotIn("brain.", hit["next_action"])
        _assert_elapsed_meta(self, payload)
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_search_covers_beads_and_evidence(self) -> None:
        bead = SEARCH.search_payload("local search", adapters=_adapters(), kind_filter=["bead"])
        evidence = SEARCH.search_payload("invalid config", evidence=_evidence(), source_filter=["evidence"])

        self.assertEqual(bead["hits"][0]["id"], "skillbox-agent-ops-search-xeu")
        self.assertEqual(bead["hits"][0]["kind"], "bead")
        self.assertEqual(evidence["hits"][0]["source"], "evidence")
        self.assertIn("invalid config", evidence["hits"][0]["snippet"])

    def test_empty_query_returns_invalid_argument(self) -> None:
        payload = SEARCH.search_payload("   ")

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")
        _assert_error_envelope(self, payload, "INVALID_ARGUMENT")
        self.assertTrue(payload["examples"])
        self.assertTrue(all(example.startswith("python3 .env-manager/manage.py ") for example in payload["examples"]))
        _assert_elapsed_meta(self, payload)

    def test_source_and_kind_filters_are_applied(self) -> None:
        payload = SEARCH.search_payload(
            "api",
            graph=_graph(),
            docs={"README.md": "api docs"},
            source_filter=["graph"],
            kind_filter=["service"],
        )

        self.assertTrue(payload["hits"])
        self.assertTrue(all(hit["source"] == "graph" for hit in payload["hits"]))
        self.assertTrue(all(hit["kind"] == "service" for hit in payload["hits"]))

    def test_empty_search_returns_related_suggestions(self) -> None:
        payload = SEARCH.search_payload(
            "apu",
            graph=_graph(),
            docs={"README.md": "unrelated docs only"},
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 0)
        suggestion_ids = [item["id"] for item in payload.get("suggestions") or []]
        self.assertIn("service:api", suggestion_ids)
        lines = search_brain_text_lines(payload)
        self.assertTrue(any("service:api" in line for line in lines))
        self.assertTrue(any("manage.py search service:api" in line for line in lines))

    def test_missing_doc_source_and_missing_filter_source_emit_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = SEARCH.search_payload(
                "runtime",
                root_dir=Path(tmp),
                doc_paths=["README.md", "docs/missing.md"],
                source_filter=["docs", "not-present"],
            )

        warning_codes = [warning["code"] for warning in payload["warnings"]]
        self.assertIn("MISSING_SOURCE", warning_codes)
        self.assertTrue(any(warning.get("source") == "not-present" for warning in payload["warnings"]))


if __name__ == "__main__":
    unittest.main()
