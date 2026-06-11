from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import agent_graph as GRAPH  # noqa: E402
from runtime_manager.runtime_ops import order_service_ids  # noqa: E402


def _minimal_model() -> dict[str, object]:
    return {
        "active_profiles": ["core"],
        "active_clients": ["personal"],
        "clients": [{"id": "personal", "label": "Personal"}],
        "profiles": [{"id": "core", "label": "Core"}],
        "repos": [{"id": "app", "host_path": "/repo/app", "profiles": ["core"]}],
        "artifacts": [{"id": "bundle", "path": "/tmp/bundle.tgz"}],
        "services": [
            {"id": "db", "kind": "service", "profiles": ["core"]},
            {
                "id": "api",
                "kind": "service",
                "depends_on": ["db"],
                "bootstrap_tasks": ["build-api"],
                "repo": "app",
                "artifact": "bundle",
                "profiles": ["core"],
            },
            {"id": "memory-mcp", "kind": "mcp", "mcp_server": "memory", "profiles": ["core"]},
        ],
        "tasks": [
            {"id": "prepare", "repo": "app", "profiles": ["core"]},
            {"id": "build-api", "depends_on": ["prepare"], "repo": "app", "profiles": ["core"]},
        ],
        "checks": [{"id": "runtime-doctor", "type": "command", "repo": "app", "profiles": ["core"]}],
        "skill_repos": [{"id": "skills", "path": "/repo/skills", "profiles": ["core"]}],
    }


def _adapter_payload() -> dict[str, object]:
    return {
        "br_ready": {
            "source": "br",
            "ok": True,
            "status": "ok",
            "payload": [
                {
                    "id": "bd-1",
                    "title": "Fix MCP drift",
                    "status": "open",
                    "priority": 1,
                    "issue_type": "bug",
                }
            ],
            "warnings": [],
        },
        "sbp_skills": {
            "source": "sbp",
            "ok": False,
            "status": "unavailable",
            "warnings": [{"code": "UNAVAILABLE_DEPENDENCY", "message": "sbp missing"}],
        },
    }


class AgentGraphTests(unittest.TestCase):
    def test_graph_builder_includes_phase_a_nodes_edges_and_adapter_evidence(self) -> None:
        payload = GRAPH.build_agent_graph_payload(_minimal_model(), adapters=_adapter_payload())
        node_ids = {node["id"] for node in payload["nodes"]}
        edge_keys = {(edge["source"], edge["kind"], edge["target"]) for edge in payload["edges"]}

        for expected in (
            "client:personal",
            "profile:core",
            "repo:app",
            "artifact:bundle",
            "service:api",
            "service:db",
            "task:build-api",
            "task:prepare",
            "check:runtime-doctor",
            "skill:skills",
            "mcp_tool:skillbox",
            "mcp_tool:memory",
            "command:brain.next",
            "mcp_tool:skillbox_next",
            "bead:bd-1",
            "evidence:adapters",
            "snapshot:current",
        ):
            self.assertIn(expected, node_ids)

        self.assertIn(("service:api", "depends_on", "service:db"), edge_keys)
        self.assertIn(("service:api", "depends_on", "task:build-api"), edge_keys)
        self.assertIn(("task:build-api", "depends_on", "task:prepare"), edge_keys)
        self.assertIn(("service:api", "configured_by", "repo:app"), edge_keys)
        self.assertIn(("task:build-api", "configured_by", "repo:app"), edge_keys)
        self.assertIn(("service:api", "consumes", "artifact:bundle"), edge_keys)
        self.assertIn(("command:brain.next", "exposes", "mcp_tool:skillbox_next"), edge_keys)
        self.assertTrue(any(w["code"] == "UNAVAILABLE_DEPENDENCY" for w in payload["warnings"]))
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_service_cycle_warning_agrees_with_runtime_ops(self) -> None:
        model = {
            "active_profiles": [],
            "active_clients": [],
            "services": [
                {"id": "a", "depends_on": ["b"]},
                {"id": "b", "depends_on": ["a"]},
            ],
            "tasks": [],
            "repos": [],
            "artifacts": [],
            "checks": [],
        }
        with self.assertRaises(RuntimeError) as raised:
            order_service_ids(model, {"a", "b"})

        payload = GRAPH.build_agent_graph_payload(model)
        warning = next(w for w in payload["warnings"] if w["code"] == "SERVICE_DEPENDENCY_CYCLE")
        self.assertEqual(warning["message"], str(raised.exception))

    def test_adapter_failure_becomes_warning_not_crash(self) -> None:
        payload = GRAPH.build_agent_graph_payload(
            _minimal_model(),
            adapters={
                "bv_triage": {
                    "source": "bv",
                    "ok": False,
                    "status": "timeout",
                    "warnings": [{"code": "ADAPTER_TIMEOUT", "message": "bv timed out"}],
                }
            },
        )

        self.assertFalse(payload["ok"])
        self.assertTrue(any(w["code"] == "ADAPTER_TIMEOUT" for w in payload["warnings"]))


if __name__ == "__main__":
    unittest.main()
