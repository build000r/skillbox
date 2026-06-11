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

from runtime_manager import agent_snapshots as SNAP  # noqa: E402


def _status(state: str = "running") -> dict[str, object]:
    return {
        "active_clients": ["personal"],
        "active_profiles": ["core"],
        "services": [{"id": "api", "state": state, "token": "secret-value"}],
        "tasks": [],
        "repos": [],
        "blocked_services": [],
    }


def _doctor(status: str = "pass") -> dict[str, object]:
    return {
        "checks": [
            {
                "code": "runtime",
                "status": status,
                "message": "AUTH_TOKEN=supersecret runtime check",
            }
        ]
    }


def _evidence(overall: str = "green") -> dict[str, object]:
    return {
        "overall": overall,
        "blocked_conditions": ["doctor: 1 check failing"] if overall == "red" else [],
        "next_actions": ["status --format json"],
        "sections": {"env": {"api_key": "abc123"}},
    }


def _graph(extra_warning: bool = False) -> dict[str, object]:
    return {
        "nodes": [
            {"id": "service:api", "kind": "service", "label": "api"},
            {"id": "service:db", "kind": "service", "label": "db"},
        ],
        "edges": [{"source": "service:api", "target": "service:db", "kind": "depends_on"}],
        "warnings": [{"code": "SERVICE_DEPENDENCY_CYCLE", "message": "cycle"}] if extra_warning else [],
    }


class AgentSnapshotTests(unittest.TestCase):
    def test_create_snapshot_is_deterministic_and_redacts_secrets(self) -> None:
        first = SNAP.create_snapshot_payload(
            status=_status(),
            doctor=_doctor(),
            evidence=_evidence(),
            graph=_graph(),
            label="fixture",
            created_at="2026-06-11T00:00:00Z",
        )
        second = SNAP.create_snapshot_payload(
            status=_status(),
            doctor=_doctor(),
            evidence=_evidence(),
            graph=_graph(),
            label="fixture",
            created_at="2026-06-11T00:00:00Z",
        )

        self.assertEqual(first, second)
        encoded = json.dumps(first, sort_keys=True)
        self.assertNotIn("supersecret", encoded)
        self.assertNotIn("abc123", encoded)
        self.assertIn("[REDACTED]", encoded)
        self.assertEqual(json.loads(json.dumps(first)), first)

    def test_save_load_and_replay_snapshot_use_state_storage(self) -> None:
        payload = SNAP.create_snapshot_payload(
            status=_status(),
            doctor=_doctor(),
            evidence=_evidence(),
            graph=_graph(),
            created_at="2026-06-11T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = SNAP.save_snapshot(Path(tmp), payload)
            loaded = SNAP.load_snapshot(path)

        replay = SNAP.replay_snapshot(loaded)

        self.assertEqual(loaded, payload)
        self.assertEqual(replay["summary"]["services"], 1)
        self.assertEqual(replay["summary"]["graph_nodes"], 2)

    def test_diff_reports_changed_entities_and_severity(self) -> None:
        before = SNAP.create_snapshot_payload(
            status=_status("running"),
            doctor=_doctor("pass"),
            evidence=_evidence("green"),
            graph=_graph(False),
            created_at="2026-06-11T00:00:00Z",
        )
        after = SNAP.create_snapshot_payload(
            status=_status("stopped"),
            doctor=_doctor("fail"),
            evidence=_evidence("red"),
            graph=_graph(True),
            created_at="2026-06-11T00:05:00Z",
        )

        diff = SNAP.diff_snapshots(before, after)
        entities = {change["entity"]: change for change in diff["changes"]}

        self.assertGreaterEqual(diff["severity_counts"]["high"], 3)
        self.assertEqual(entities["status.service:api"]["severity"], "high")
        self.assertEqual(entities["doctor.check:runtime"]["severity"], "high")
        self.assertEqual(entities["evidence.overall"]["severity"], "high")
        self.assertIn("graph.warning:SERVICE_DEPENDENCY_CYCLE:cycle", entities)

    def test_replay_committed_fixture_without_live_services(self) -> None:
        fixture = SNAP.load_snapshot(ROOT_DIR / "tests" / "goldens" / "agent_ops_snapshot.json")
        replay = SNAP.replay_snapshot(fixture)

        self.assertTrue(replay["ok"])
        self.assertEqual(replay["snapshot_id"], "golden-fixture")
        self.assertEqual(replay["summary"]["overall"], "green")
        self.assertEqual(replay["summary"]["graph_edges"], 1)


if __name__ == "__main__":
    unittest.main()
