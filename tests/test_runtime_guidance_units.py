from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import shared as SHARED  # noqa: E402


class RuntimeGuidanceUnitTests(unittest.TestCase):
    def test_classify_error_maps_known_message_patterns_to_recovery_payloads(self) -> None:
        cases = [
            (
                "client-init requires a client_id",
                "client-init",
                "missing_argument",
                ["client-init --list-blueprints --format json"],
            ),
            (
                "Blueprint service not found",
                "client-init",
                "blueprint_not_found",
                ["client-init --list-blueprints --format json"],
            ),
            ("missing required values: SERVICE_ID", "client-init", "missing_variable", None),
            ("non-projection output directory already exists", "client-project", "conflict", None),
            ("target repo has a dirty working tree", "client-publish", "conflict", None),
            (
                "no private publish target configured for client",
                "client-publish",
                "missing_target_repo",
                ["private-init --format json"],
            ),
            ("target must be a git repo", "client-publish", "invalid_target_repo", None),
            ("env file /tmp/.env is missing", "sync", "missing_env_file", ["sync --format json"]),
            (
                "api failed to become healthy after 60s",
                "up",
                "service_health_failure",
                ["logs --format json", "doctor --format json"],
            ),
            ("Invalid client id 'Bad_Client'", "client-init", "invalid_client_id", None),
            (
                "unknown client scaffold pack enterprise",
                "client-init",
                "invalid_scaffold_pack",
                ["client-init --list-blueprints --format json"],
            ),
            ("session_id is required", "session-event", "missing_argument", None),
            (
                "Session not found: missing-session",
                "session-status",
                "session_not_found",
                ["session-status <client> --format json", "focus <client> --format json"],
            ),
            (
                "Unsupported session status 'paused'",
                "session-end",
                "session_state_conflict",
                ["session-status <client> --format json"],
            ),
        ]

        for message, command, expected_type, expected_actions in cases:
            with self.subTest(message=message):
                payload = SHARED.classify_error(RuntimeError(message), command)
                self.assertEqual(payload["error"]["type"], expected_type)
                self.assertTrue(payload["error"]["recoverable"])
                self.assertIn("recovery_hint", payload["error"])
                if expected_actions is not None:
                    self.assertEqual(payload["next_actions"], expected_actions)

    def test_classify_error_handles_persistence_codes_and_fallback_commands(self) -> None:
        persistence = SHARED.PersistenceContractError("STATE_ROOT_MISSING", "state root missing")
        persistence_payload = SHARED.classify_error(persistence, "render")
        self.assertEqual(persistence_payload["error"]["type"], "STATE_ROOT_MISSING")
        self.assertEqual(persistence_payload["next_actions"], ["render --format json", "doctor --format json"])

        sync_payload = SHARED.classify_error(RuntimeError("unexpected sync failure"), "sync")
        self.assertEqual(sync_payload["error"]["type"], "runtime_error")
        self.assertEqual(sync_payload["next_actions"], ["doctor --format json", "status --format json"])

        unknown_payload = SHARED.classify_error(RuntimeError("unexpected worker failure"), "unknown")
        self.assertEqual(unknown_payload["next_actions"], ["doctor --format json"])

    def test_next_actions_for_status_prioritizes_missing_repos_tasks_and_services(self) -> None:
        payload = {
            "repos": [{"id": "repo", "present": False}],
            "tasks": [{"id": "bootstrap", "state": "pending"}],
            "services": [{"id": "api", "state": "not-running"}],
        }
        self.assertEqual(
            SHARED.next_actions_for_status(payload),
            ["sync --format json", "bootstrap --format json", "up --format json"],
        )
        self.assertEqual(
            SHARED.next_actions_for_status({"services": [{"state": "stopped"}]}),
            ["up --format json"],
        )
        self.assertEqual(SHARED.next_actions_for_status({"services": [], "tasks": [], "repos": []}), ["doctor --format json"])

    def test_next_actions_for_doctor_and_focus_cover_recovery_paths(self) -> None:
        fail = SHARED.CheckResult(status="fail", code="api", message="api down")
        warn = SHARED.CheckResult(status="warn", code="disk", message="disk low")
        ok = SHARED.CheckResult(status="ok", code="context", message="ready")

        self.assertEqual(SHARED.next_actions_for_doctor([fail]), ["sync --format json", "status --format json"])
        self.assertEqual(SHARED.next_actions_for_doctor([warn]), ["sync --format json"])
        self.assertEqual(SHARED.next_actions_for_doctor([ok]), ["status --format json"])
        self.assertEqual(
            SHARED.next_actions_for_focus("personal", True),
            ["doctor --client personal --format json", "logs --client personal --format json"],
        )
        self.assertEqual(
            SHARED.next_actions_for_focus("personal", False, [{"id": ""}, {"id": "api"}]),
            ["status --client personal --format json", "logs --service api --client personal --format json"],
        )
        self.assertEqual(
            SHARED.next_actions_for_focus("personal", False, []),
            ["status --client personal --format json"],
        )


if __name__ == "__main__":
    unittest.main()
