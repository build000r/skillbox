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

from runtime_manager.agent_adapters import adapter_timing_summary  # noqa: E402
from runtime_manager.agent_decisions import explain_payload, next_action_payload  # noqa: E402
from runtime_manager.agent_timing import (  # noqa: E402
    COMPONENT_META_KEYS,
    PHASE_ADAPTER_COLLECTION,
    PHASE_MODEL,
    PHASE_STARTUP,
    InvocationTiming,
    attach_component_timing,
)
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

    def test_component_timing_contract_matches_golden(self) -> None:
        """Pin the timing *contract*, never timing *values*.

        Every assertion below is over key names and synthetic inputs, so the
        golden stays byte-identical across runs and across machines.
        """
        golden = json.loads((ROOT_DIR / "tests" / "goldens" / "agent_ops_brain_surfaces.json").read_text())
        timing_golden = golden["timing"]

        self.assertEqual(list(COMPONENT_META_KEYS), timing_golden["component_meta_keys"])
        self.assertEqual(
            sorted({PHASE_ADAPTER_COLLECTION, PHASE_MODEL, PHASE_STARTUP}),
            timing_golden["phase_names"],
        )
        self.assertEqual(
            sorted(adapter_timing_summary({}, collection_ms=0.0)),
            timing_golden["adapter_summary_keys"],
        )

        recorder = InvocationTiming()
        recorder.record(PHASE_STARTUP, 900.0)
        recorder.record(PHASE_MODEL, 250.0)
        recorder.record(PHASE_ADAPTER_COLLECTION, 7500.0)
        payload = attach_component_timing(
            {"ok": True, "meta": {"elapsed_ms": 1.7}},
            invocation=recorder,
            end_to_end_ms=8700.0,
        )
        meta = payload["meta"]

        self.assertEqual(sorted(meta), timing_golden["required_meta_keys"])
        self.assertEqual(sorted(meta["timing"]), timing_golden["timing_block_keys"])
        # The regression this contract exists to prevent: a payload that reports
        # 1.7ms while the invocation actually took 8.7 seconds.
        self.assertEqual(meta["compute_ms"], 1.7)
        self.assertEqual(meta["end_to_end_ms"], 8700.0)
        self.assertEqual(meta["adapter_collection_ms"], 7500.0)
        self.assertEqual(meta["model_ms"], 250.0)
        self.assertEqual(meta["startup_ms"], 900.0)

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
