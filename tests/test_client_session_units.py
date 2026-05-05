from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import shared as SHARED  # noqa: E402


def _write_runtime_root(root: Path) -> None:
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    (root / ".env.example").write_text(
        "\n".join(
            [
                "SKILLBOX_WORKSPACE_ROOT=/workspace",
                "SKILLBOX_CLIENTS_ROOT=/workspace/workspace/clients",
                "SKILLBOX_CLIENTS_HOST_ROOT=./clients",
                "SKILLBOX_LOG_ROOT=/workspace/logs",
                "SKILLBOX_HOME_ROOT=/home/sandbox",
                "SKILLBOX_MONOSERVER_ROOT=/monoserver",
                "SKILLBOX_STATE_ROOT=./.skillbox-state",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "workspace" / "runtime.yaml").write_text(
        "version: 2\n"
        "selection: {}\n"
        "clients:\n"
        "  - id: personal\n"
        "    default_cwd: /monoserver/personal\n"
        "  - id: other\n"
        "    default_cwd: /monoserver/other\n"
        "core:\n"
        "  repos: []\n"
        "  artifacts: []\n"
        "  skills: []\n"
        "  services: []\n"
        "  logs: []\n"
        "  checks: []\n",
        encoding="utf-8",
    )
    (root / "workspace" / "persistence.yaml").write_text(
        "version: 1\n"
        "state_root_env: SKILLBOX_STATE_ROOT\n"
        "targets:\n"
        "  local:\n"
        "    provider: local\n"
        "    default_state_root: ./.skillbox-state\n"
        "bindings: []\n",
        encoding="utf-8",
    )


def _write_client_overlay(root: Path, client_id: str = "personal") -> None:
    client_dir = root / "clients" / client_id
    client_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "overlay.yaml").write_text(
        "version: 1\n"
        "client:\n"
        f"  id: {client_id}\n"
        f"  label: {client_id.title()}\n"
        f"  default_cwd: /monoserver/{client_id}\n"
        "  context:\n"
        f"    cwd_match: [/monoserver/{client_id}]\n",
        encoding="utf-8",
    )


class ClientSessionUnitTests(unittest.TestCase):
    def test_session_lifecycle_persists_events_handoff_and_status_payloads(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(SHARED, "_generate_session_id", return_value="20260505-010203-a1b2c3"),
            mock.patch.object(SHARED, "_session_now", side_effect=[100.0, 101.0, 102.0, 103.0, 104.0, 105.0]),
        ):
            root = Path(tmpdir)
            _write_runtime_root(root)
            _write_client_overlay(root)

            started = SHARED.start_client_session(
                root,
                "personal",
                label="Tutoring run",
                cwd="/monoserver/personal/app",
                goal="Ship the next slice",
                actor="coach",
            )
            session_id = started["session"]["session_id"]
            session_dir = SHARED.session_paths(root, "personal", session_id)["session_dir"]

            self.assertEqual(session_id, "20260505-010203-a1b2c3")
            self.assertEqual(started["client_id"], "personal")
            self.assertEqual(started["session"]["status"], "active")
            self.assertEqual(started["session"]["event_count"], 1)
            self.assertEqual(started["session"]["last_event_type"], "session.started")
            self.assertEqual(started["session"]["recent_events"][0]["detail"]["actor"], "coach")
            self.assertTrue((session_dir / "meta.json").is_file())
            self.assertTrue((session_dir / "events.jsonl").is_file())
            self.assertTrue((session_dir / "handoff.md").is_file())

            noted = SHARED.append_client_session_event(
                root,
                "personal",
                session_id,
                event_type="note",
                message="Student took over implementation",
                detail={"actor": "coach"},
            )
            self.assertEqual(noted["session"]["event_count"], 2)
            self.assertEqual(noted["session"]["last_event_type"], "session.note")
            self.assertEqual(noted["session"]["last_message"], "Student took over implementation")

            ended = SHARED.end_client_session(
                root,
                "personal",
                session_id,
                final_status="failed",
                summary="Container crashed mid-run",
            )
            self.assertEqual(ended["session"]["status"], "failed")
            self.assertEqual(ended["session"]["summary"], "Container crashed mid-run")
            self.assertEqual(ended["session"]["event_count"], 3)
            self.assertEqual(ended["session"]["recent_events"][-1]["type"], "session.ended")
            self.assertIn("Container crashed mid-run", (session_dir / "handoff.md").read_text(encoding="utf-8"))

            resumed = SHARED.resume_client_session(
                root,
                "personal",
                session_id,
                actor="coach",
                message="Recovered after crash",
            )
            self.assertEqual(resumed["session"]["status"], "active")
            self.assertEqual(resumed["session"]["resume_count"], 1)
            self.assertEqual(resumed["session"]["last_resumed_from"], "failed")
            self.assertEqual(resumed["session"]["last_event_type"], "session.resumed")
            self.assertEqual(resumed["session"]["last_message"], "Recovered after crash")
            self.assertEqual(resumed["session"]["event_count"], 4)

            status_by_id = SHARED.session_status_payload(root, "personal", session_id=session_id)
            self.assertEqual(status_by_id["session"]["session_id"], session_id)
            status_list = SHARED.session_status_payload(root, "personal", limit=10)
            self.assertEqual(status_list["count"], 1)
            self.assertEqual(status_list["sessions"][0]["session_id"], session_id)

            meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["last_heartbeat_at"], 105.0)
            self.assertNotIn("paths", meta)
            self.assertNotIn("recent_events", meta)
            event_types = [json.loads(line)["type"] for line in (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(event_types, ["session.started", "session.note", "session.ended", "session.resumed"])
            runtime_log = (root / "logs" / "runtime" / "runtime.log").read_text(encoding="utf-8")
            self.assertIn("session.resumed personal:20260505-010203-a1b2c3", runtime_log)

    def test_list_client_sessions_orders_limits_and_filters_bad_metadata(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(SHARED, "_generate_session_id", side_effect=["old-session", "new-session"]),
            mock.patch.object(SHARED, "_session_now", side_effect=[10.0, 11.0, 20.0, 21.0]),
        ):
            root = Path(tmpdir)
            _write_runtime_root(root)
            _write_client_overlay(root)

            old = SHARED.start_client_session(root, "personal", label="Old")["session"]["session_id"]
            new = SHARED.start_client_session(root, "personal", label="New")["session"]["session_id"]
            paths = SHARED.session_paths(root, "personal", new)
            stale_dir = paths["sessions_root"] / "stale"
            stale_dir.mkdir(parents=True)
            (stale_dir / "meta.json").write_text("{bad json\n", encoding="utf-8")
            wrong_dir = paths["sessions_root"] / "wrong-client"
            wrong_dir.mkdir()
            SHARED.write_json_file(
                wrong_dir / "meta.json",
                {
                    "session_id": "wrong-client",
                    "client_id": "other",
                    "status": "active",
                    "updated_at": 99.0,
                },
            )

            latest_only = SHARED.list_client_sessions(root, "personal", limit=1)
            all_sessions = SHARED.list_client_sessions(root, "personal", limit=0)

            self.assertEqual([item["session_id"] for item in latest_only], [new])
            self.assertEqual([item["session_id"] for item in all_sessions], [new, old])
            self.assertNotIn("recent_events", latest_only[0])

    def test_session_lifecycle_rejects_missing_ids_and_invalid_transitions(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(SHARED, "_generate_session_id", return_value="transition-session"),
            mock.patch.object(SHARED, "_session_now", side_effect=[1.0, 2.0, 3.0, 4.0]),
        ):
            root = Path(tmpdir)
            _write_runtime_root(root)
            _write_client_overlay(root)
            session_id = SHARED.start_client_session(root, "personal")["session"]["session_id"]

            with self.assertRaises(RuntimeError) as missing_id:
                SHARED.read_client_session(root, "personal", "")
            self.assertIn("session_id is required", str(missing_id.exception))

            with self.assertRaises(RuntimeError) as bad_event_type:
                SHARED.append_client_session_event(root, "personal", session_id, event_type="")
            self.assertIn("event_type is required", str(bad_event_type.exception))

            with self.assertRaises(RuntimeError) as unsupported_status:
                SHARED.end_client_session(root, "personal", session_id, final_status="paused")
            self.assertIn("Unsupported session status", str(unsupported_status.exception))

            with self.assertRaises(RuntimeError) as active_resume:
                SHARED.resume_client_session(root, "personal", session_id)
            self.assertIn("already active", str(active_resume.exception))

            SHARED.end_client_session(root, "personal", session_id)
            with self.assertRaises(RuntimeError) as inactive_event:
                SHARED.append_client_session_event(root, "personal", session_id, event_type="note")
            self.assertIn("not active", str(inactive_event.exception))
            with self.assertRaises(RuntimeError) as inactive_end:
                SHARED.end_client_session(root, "personal", session_id)
            self.assertIn("not active", str(inactive_end.exception))

    def test_read_session_events_ignores_empty_and_malformed_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "events.jsonl"
            events_path.write_text(
                "\n"
                "{bad json\n"
                '{"type": "session.started"}\n'
                "[]\n"
                '{"type": "session.note"}\n',
                encoding="utf-8",
            )

            self.assertEqual(
                [event["type"] for event in SHARED._read_session_events(events_path, limit=1)],
                ["session.note"],
            )
            self.assertEqual(
                [event["type"] for event in SHARED._read_session_events(events_path, limit=0)],
                ["session.started", "session.note"],
            )
            self.assertEqual(SHARED._read_session_events(Path(tmpdir) / "missing.jsonl"), [])

    def test_read_durable_session_events_uses_meta_fallbacks_and_scope_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_runtime_root(root)
            _write_client_overlay(root)
            sessions_root = SHARED.resolve_client_session_root(root, "personal")
            first_dir = sessions_root / "first"
            second_dir = sessions_root / "second"
            first_dir.mkdir(parents=True)
            second_dir.mkdir()
            SHARED.write_json_file(
                first_dir / "meta.json",
                {"client_id": "personal", "session_id": "first"},
            )
            (first_dir / "events.jsonl").write_text(
                "\n"
                "{bad json\n"
                "[]\n"
                '{"ts": 10, "type": "session.note", "detail": "not-a-map", "message": "ignored"}\n'
                '{"ts": 11, "type": "session.note", "detail": {"message": "from meta"}}\n',
                encoding="utf-8",
            )
            (second_dir / "meta.json").write_text("{bad json\n", encoding="utf-8")
            (second_dir / "events.jsonl").write_text(
                '{"ts": 12, "type": "session.note", "client_id": "other", "detail": {"message": "fallback"}}\n',
                encoding="utf-8",
            )

            all_events = SHARED.read_durable_session_events(root)
            first_events = SHARED.read_durable_session_events(root, session_id="first")
            personal_events = SHARED.read_durable_session_events(root, client_id="personal")
            missing_client_events = SHARED.read_durable_session_events(root, client_id="missing")

            self.assertEqual([event["session_id"] for event in all_events], ["first", "first", "second"])
            self.assertEqual(all_events[0]["detail"], {})
            self.assertEqual(all_events[1]["message"], "from meta")
            self.assertEqual(all_events[2]["client_id"], "other")
            self.assertEqual([event["session_id"] for event in first_events], ["first", "first"])
            self.assertEqual([event["session_id"] for event in personal_events], ["first", "first", "second"])
            self.assertEqual(missing_client_events, [])


if __name__ == "__main__":
    unittest.main()
