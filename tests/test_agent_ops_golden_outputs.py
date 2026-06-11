from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.agent_decisions import explain_payload, next_action_payload  # noqa: E402
from runtime_manager.agent_graph_engine import graph_command_payload  # noqa: E402
from runtime_manager.agent_snapshots import load_snapshot, replay_snapshot  # noqa: E402
from runtime_manager.command_registry import registry_payload  # noqa: E402
from runtime_manager.context_rendering import generate_context_markdown  # noqa: E402


def _fixture_graph() -> dict[str, object]:
    return {
        "ok": True,
        "nodes": [
            {"id": "service:db", "kind": "service", "label": "db", "attrs": {}},
            {"id": "service:api", "kind": "service", "label": "api", "attrs": {}},
            {"id": "check:smoke", "kind": "check", "label": "smoke", "attrs": {}},
            {"id": "command:brain.next", "kind": "command", "label": "next", "attrs": {}},
            {"id": "mcp_tool:skillbox_next", "kind": "mcp_tool", "label": "skillbox_next", "attrs": {}},
        ],
        "edges": [
            {"source": "service:api", "target": "service:db", "kind": "depends_on", "attrs": {}},
            {"source": "check:smoke", "target": "service:api", "kind": "depends_on", "attrs": {}},
            {"source": "command:brain.next", "target": "mcp_tool:skillbox_next", "kind": "exposes", "attrs": {}},
        ],
        "warnings": [],
    }


def _fixture_adapters() -> dict[str, object]:
    return {
        "br_ready": {
            "ok": True,
            "payload": [{"id": "ready-1", "title": "Ready issue", "priority": 1}],
            "warnings": [],
        },
        "bv_triage": {
            "ok": True,
            "payload": {
                "recommendations": [
                    {"id": "ready-1", "claim_command": "br update ready-1 --status=in_progress"}
                ]
            },
            "warnings": [],
        },
    }


class AgentOpsGoldenOutputTests(unittest.TestCase):
    def test_agent_ops_surface_golden_contract(self) -> None:
        golden = json.loads((ROOT_DIR / "tests" / "goldens" / "agent_ops_brain_surfaces.json").read_text())
        registry = registry_payload()
        registry_entries = {entry["id"]: entry for entry in registry["capabilities"]}
        graph = graph_command_payload(_fixture_graph(), algorithm="critical-path")
        next_payload = next_action_payload(
            _fixture_graph(),
            adapters=_fixture_adapters(),
            evidence={"overall": "green", "blocked_conditions": []},
        )
        explain = explain_payload(_fixture_graph(), "brain.next", adapters=_fixture_adapters())
        replay = replay_snapshot(load_snapshot(ROOT_DIR / "tests" / "goldens" / "agent_ops_snapshot.json"))

        self.assertTrue(set(golden["capabilities"]["registry_ids"]) <= set(registry_entries))
        self.assertEqual(
            [registry_entries[item]["mcp_tool"] for item in golden["capabilities"]["registry_ids"]],
            golden["capabilities"]["mcp_tools"],
        )
        self.assertEqual(graph["graph"]["node_count"], golden["graph"]["node_count"])
        self.assertEqual(graph["graph"]["edge_count"], golden["graph"]["edge_count"])
        self.assertEqual(graph["algorithm"]["name"], golden["graph"]["algorithm"])
        self.assertEqual(graph["algorithm"]["result"]["path"], golden["graph"]["critical_path"])
        self.assertEqual(next_payload["recommendations"][0]["id"], golden["next"]["top_id"])
        self.assertEqual(next_payload["recommendations"][0]["commands"][0], golden["next"]["top_command"])
        self.assertEqual(next_payload["recommendations"][0]["reasons"][0], golden["next"]["top_reason"])
        self.assertEqual(explain["target"], golden["explain"]["target"])
        self.assertEqual(explain["kind"], golden["explain"]["kind"])
        self.assertEqual(replay["snapshot_id"], golden["snap_replay"]["snapshot_id"])
        self.assertEqual(replay["summary"]["overall"], golden["snap_replay"]["overall"])
        self.assertEqual(replay["summary"]["graph_nodes"], golden["snap_replay"]["graph_nodes"])
        self.assertEqual(replay["summary"]["graph_edges"], golden["snap_replay"]["graph_edges"])

    def test_fixture_scale_surfaces_are_fast(self) -> None:
        started = time.monotonic()
        graph_command_payload(_fixture_graph(), algorithm="all")
        next_action_payload(_fixture_graph(), adapters=_fixture_adapters())
        explain_payload(_fixture_graph(), "brain.next", adapters=_fixture_adapters())
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.25)

    def test_generated_context_points_agents_to_capabilities_then_next(self) -> None:
        context = generate_context_markdown(
            {
                "active_clients": [],
                "active_profiles": ["core"],
                "root_dir": str(ROOT_DIR),
                "clients": [],
                "repos": [],
                "services": [],
                "tasks": [],
                "skills": [],
                "logs": [],
            }
        )

        self.assertIn("python3 .env-manager/manage.py capabilities --json", context)
        self.assertIn("python3 .env-manager/manage.py next --format json", context)


if __name__ == "__main__":
    unittest.main()
