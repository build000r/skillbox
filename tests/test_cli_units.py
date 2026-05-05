from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import cli as CLI  # noqa: E402


def _ns(**kwargs: object) -> argparse.Namespace:
    defaults = {
        "format": "json",
        "dry_run": False,
        "force": False,
        "client": [],
        "profile": [],
        "service": [],
        "wait_seconds": 0.0,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class CliUnitTests(unittest.TestCase):
    def test_overlay_handler_covers_persistent_activate_and_unlink_paths(self) -> None:
        state: set[str] = set()
        emitted: list[dict[str, object]] = []

        def set_overlay(name: str, enabled: bool) -> None:
            state.add(name) if enabled else state.discard(name)

        def toggle_overlay(name: str) -> None:
            state.remove(name) if name in state else state.add(name)

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(CLI, "active_overlays", side_effect=lambda: set(state)),
            mock.patch.object(CLI, "set_overlay", side_effect=set_overlay),
            mock.patch.object(CLI, "toggle_overlay", side_effect=toggle_overlay),
            mock.patch.object(CLI, "unlink_overlay_scoped_skills", return_value=["one", "two"]),
            mock.patch.object(
                CLI,
                "activate_overlay_scoped_skills",
                return_value=[
                    {
                        "skill": "demo",
                        "activation_packet": {
                            "name": "demo",
                            "source": "overlay",
                            "skill_md_sha256": "abc",
                            "skill_md": "# Demo\n",
                        },
                    }
                ],
            ),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            root = Path(tmpdir)
            on_args = _ns(action="on", name="marketing", cwd=str(root), keep=False, to="project", scope="project")
            off_args = _ns(action="off", name="marketing", cwd=str(root), keep=False, to="project", scope="project")
            activate_args = _ns(
                action="activate",
                name="marketing",
                cwd=str(root),
                keep=False,
                to="project",
                scope="project",
                category=[],
                source=None,
            )

            self.assertEqual(CLI._handle_overlay(on_args, root, {}, "reuse"), CLI.EXIT_OK)
            self.assertEqual(CLI._handle_overlay(off_args, root, {}, "reuse"), CLI.EXIT_OK)
            self.assertEqual(CLI._handle_overlay(activate_args, root, {}, "reuse"), CLI.EXIT_OK)

        self.assertEqual(emitted[0]["overlays"], ["marketing"])
        self.assertEqual(emitted[1]["unlinked"], ["one", "two"])
        self.assertEqual(emitted[2]["activations"][0]["skill"], "demo")

        with self.assertRaises(RuntimeError):
            CLI._overlay_action_and_name(_ns(action="on", name=""))

        text = StringIO()
        with redirect_stdout(text):
            CLI._print_overlay_text(
                {
                    "action": "activate",
                    "name": "marketing",
                    "overlays": ["marketing"],
                    "unlinked": ["one"],
                    "activations": [
                        {"skill": "missing", "activation_packet": None},
                        {"skill": "demo", "activation_packet": {"name": "demo", "source": "overlay", "skill_md_sha256": "abc", "skill_md": "# Demo\n"}},
                    ],
                }
            )
        self.assertIn("activated: 2 skills", text.getvalue())
        self.assertIn("activation packet: missing unavailable", text.getvalue())

    def test_operator_booking_text_lines_cover_each_action(self) -> None:
        config_lines = CLI._operator_booking_text_lines(
            {
                "action": "config",
                "operator_booking": {
                    "client_id": "personal",
                    "availability_url": "https://book.test/availability",
                    "booking_hold_url": "https://book.test/hold",
                    "magic_link_url": "https://book.test/magic",
                    "api_key_env": "BOOK_API_KEY",
                    "api_key_configured": True,
                    "access_token_env": "BOOK_ACCESS_TOKEN",
                    "access_token_configured": False,
                },
            }
        )
        availability_lines = CLI._operator_booking_text_lines(
            {
                "action": "availability",
                "client_id": "personal",
                "booking_url": "https://book.test",
                "timezone": "UTC",
                "available": 1,
                "slots": [{"date": "2026-05-05", "slot": "am", "price": 500}],
            }
        )
        dry_run_lines = CLI._operator_booking_text_lines(
            {
                "action": "book",
                "dry_run": True,
                "booking_url": "https://book.test/hold",
                "magic_link_url": "https://book.test/magic",
            }
        )
        booked_lines = CLI._operator_booking_text_lines(
            {
                "action": "book",
                "booking": {
                    "bookingId": "bk_1",
                    "resourceKey": "operator",
                    "actionKey": "hold",
                    "priceDisplay": "$500",
                },
                "magic_link": {"email": "a@example.test"},
                "next_actions": ["status --format json"],
            }
        )
        fallback_lines = CLI._operator_booking_text_lines({"action": "unknown", "ok": True})

        self.assertIn("operator booking: personal", config_lines)
        self.assertIn("  - 2026-05-05 am $500", availability_lines)
        self.assertIn("magic link: https://book.test/magic", dry_run_lines)
        self.assertIn("magic link sent: a@example.test", booked_lines)
        self.assertEqual(fallback_lines, ["{'action': 'unknown', 'ok': True}"])

    def test_client_private_and_session_handlers_cover_text_json_and_errors(self) -> None:
        emitted: list[dict[str, object]] = []
        root = Path("/tmp/skillbox")

        with (
            mock.patch.object(
                CLI,
                "init_private_repo",
                return_value={
                    "target_dir": "/private",
                    "clients_host_root": "/private/clients",
                    "actions": ["write overlay"],
                },
            ),
            redirect_stdout(StringIO()) as stdout,
        ):
            self.assertEqual(
                CLI._handle_private_init(_ns(path="/private", format="text"), root),
                CLI.EXIT_OK,
            )
        self.assertIn("target_dir: /private", stdout.getvalue())

        with (
            mock.patch.object(CLI, "init_private_repo", side_effect=RuntimeError("bad private")),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(
                CLI._handle_private_init(_ns(path="/private", format="json"), root),
                CLI.EXIT_ERROR,
            )
        self.assertEqual(emitted[-1]["error"]["message"], "bad private")

        project_payload = {
            "client_id": "personal",
            "output_dir": "/bundle",
            "file_count": 2,
            "payload_tree_sha256": "sha",
            "actions": ["project"],
        }
        with (
            mock.patch.object(CLI, "project_client_bundle", return_value=project_payload),
            redirect_stdout(StringIO()) as stdout,
        ):
            self.assertEqual(
                CLI._handle_client_project(  # noqa: SLF001
                    _ns(client_id="personal", profile=["core"], output_dir="/bundle", format="text"),
                    root,
                ),
                CLI.EXIT_OK,
            )
        self.assertIn("payload_tree_sha256: sha", stdout.getvalue())

        open_payload = {
            "client_id": "personal",
            "output_dir": "/surface",
            "active_profiles": ["core"],
            "mcp_servers": ["skillbox"],
            "focus": {"status": "ok"},
            "actions": ["open"],
        }
        with (
            mock.patch.object(CLI, "open_client_surface", return_value=(open_payload, CLI.EXIT_DRIFT)),
            redirect_stdout(StringIO()) as stdout,
        ):
            self.assertEqual(
                CLI._handle_client_open(  # noqa: SLF001
                    _ns(
                        client_id="personal",
                        profile=["core"],
                        output_dir="/surface",
                        from_bundle=None,
                        format="text",
                    ),
                    root,
                ),
                CLI.EXIT_DRIFT,
            )
        self.assertIn("mcp_servers: skillbox", stdout.getvalue())

        publish_payload = {
            "client_id": "personal",
            "target_dir": "/target",
            "changed": True,
            "payload_tree_sha256": "sha",
            "acceptance": {"present": True, "accepted_at": "now", "active_profiles": ["core"]},
            "deploy": {"present": True, "manifest": "deploy.json", "archive": "archive.tar.gz"},
            "commit_hash": "commit-sha",
            "actions": ["publish"],
        }
        with (
            mock.patch.object(CLI, "publish_client_bundle", return_value=publish_payload),
            redirect_stdout(StringIO()) as stdout,
        ):
            self.assertEqual(
                CLI._handle_client_publish(  # noqa: SLF001
                    _ns(
                        client_id="personal",
                        target_dir="/target",
                        from_bundle=None,
                        profile=["core"],
                        acceptance=True,
                        deploy_artifact=True,
                        commit=True,
                        format="text",
                    ),
                    root,
                ),
                CLI.EXIT_OK,
            )
        self.assertIn("deploy_manifest: deploy.json", stdout.getvalue())
        self.assertIn("commit: commit-sha", stdout.getvalue())

        with (
            mock.patch.object(CLI, "diff_client_bundle", return_value={"changed": True}),
            mock.patch.object(CLI, "print_client_diff_text") as print_diff,
        ):
            self.assertEqual(
                CLI._handle_client_diff(  # noqa: SLF001
                    _ns(client_id="personal", target_dir="/target", from_bundle=None, profile=[], format="text"),
                    root,
                ),
                CLI.EXIT_OK,
            )
        print_diff.assert_called_once_with({"changed": True})

        error_handlers = [
            (
                CLI._handle_client_project,
                "project_client_bundle",
                _ns(client_id="personal", profile=[], output_dir=None, format="json"),
                "client-project",
            ),
            (
                CLI._handle_client_open,
                "open_client_surface",
                _ns(client_id="personal", profile=[], output_dir=None, from_bundle=None, format="json"),
                "client-open",
            ),
            (
                CLI._handle_client_publish,
                "publish_client_bundle",
                _ns(
                    client_id="personal",
                    target_dir=None,
                    from_bundle=None,
                    profile=[],
                    acceptance=False,
                    deploy_artifact=False,
                    commit=False,
                    format="json",
                ),
                "client-publish",
            ),
            (
                CLI._handle_client_diff,
                "diff_client_bundle",
                _ns(client_id="personal", target_dir=None, from_bundle=None, profile=[], format="json"),
                "client-diff",
            ),
        ]
        for handler, patched_name, args, command in error_handlers:
            emitted.clear()
            with (
                mock.patch.object(CLI, patched_name, side_effect=RuntimeError(f"{command} failed")),
                mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
            ):
                self.assertEqual(handler(args, root), CLI.EXIT_ERROR)
            self.assertEqual(emitted[-1]["error"]["message"], f"{command} failed")

        session_payload = {
            "client_id": "personal",
            "session": {
                "session_id": "s1",
                "status": "active",
                "label": "Work",
                "last_event_type": "note",
                "last_message": "hello",
            },
        }
        session_handlers = [
            (
                CLI._handle_session_start,
                "start_client_session",
                _ns(client_id="personal", label="Work", cwd="/repo", goal="ship", actor="agent", format="text"),
            ),
            (
                CLI._handle_session_event,
                "append_client_session_event",
                _ns(
                    client_id="personal",
                    session_id="s1",
                    event_type="note",
                    message="hello",
                    actor="agent",
                    format="text",
                ),
            ),
            (
                CLI._handle_session_end,
                "end_client_session",
                _ns(client_id="personal", session_id="s1", status="done", summary="done", format="text"),
            ),
            (
                CLI._handle_session_resume,
                "resume_client_session",
                _ns(client_id="personal", session_id="s1", actor="agent", message="resume", format="text"),
            ),
        ]
        for handler, patched_name, args in session_handlers:
            with (
                mock.patch.object(CLI, patched_name, return_value=session_payload),
                redirect_stdout(StringIO()) as stdout,
            ):
                self.assertEqual(handler(args, root), CLI.EXIT_OK)
            self.assertIn("session: s1", stdout.getvalue())

        emitted.clear()
        with (
            mock.patch.object(CLI, "resume_client_session", side_effect=RuntimeError("resume failed")),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(
                CLI._handle_session_resume(  # noqa: SLF001
                    _ns(client_id="personal", session_id="s1", actor=None, message=None, format="json"),
                    root,
                ),
                CLI.EXIT_ERROR,
            )
        self.assertEqual(emitted[-1]["error"]["message"], "resume failed")

    def test_model_handlers_cover_sync_context_doctor_status_skills_bootstrap_and_logs(self) -> None:
        emitted: list[dict[str, object]] = []
        root = Path("/tmp/skillbox")
        model = {"services": [{"id": "api"}], "tasks": [{"id": "bootstrap"}]}

        with (
            mock.patch.object(CLI, "sync_runtime", return_value=["sync"]),
            mock.patch.object(CLI, "sync_context", return_value=["context"]),
            mock.patch.object(CLI, "resolve_context_dir", return_value=None),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_sync(_ns(context_dir=None, format="json"), root, model, "reuse"), CLI.EXIT_OK)
        self.assertEqual(emitted[-1]["actions"], ["sync", "context"])

        with (
            mock.patch.object(CLI, "sync_context", return_value=["context"]),
            mock.patch.object(CLI, "resolve_context_dir", return_value=None),
            redirect_stdout(StringIO()) as stdout,
        ):
            self.assertEqual(CLI._handle_context(_ns(context_dir=None, format="text"), root, model, "reuse"), CLI.EXIT_OK)
        self.assertIn("context", stdout.getvalue())

        fail = CLI.CheckResult("fail", "doctor", "bad", {})
        passed = CLI.CheckResult("pass", "doctor", "ok", {})
        with (
            mock.patch.object(CLI, "doctor_results", return_value=[fail, passed]),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_doctor(_ns(format="json"), root, model, "reuse"), CLI.EXIT_DRIFT)
        self.assertEqual(emitted[-1]["checks"][0]["status"], "fail")

        with (
            mock.patch.object(CLI, "runtime_status", return_value={"services": []}),
            mock.patch.object(CLI, "next_actions_for_status", return_value=["logs"]),
            mock.patch.object(CLI, "compact_runtime_status", return_value={"compact": True}),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_status(_ns(format="json", compact=True), root, model, "reuse"), CLI.EXIT_OK)
        self.assertEqual(emitted[-1], {"compact": True})

        skill_payload = {"summary": {"ok": True}}
        with (
            mock.patch.object(CLI, "collect_skill_visibility", return_value=skill_payload),
            mock.patch.object(CLI, "compact_skill_visibility_payload", return_value={"compact": True}),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(
                CLI._handle_skills(  # noqa: SLF001
                    _ns(
                        cwd="/repo",
                        no_global=False,
                        no_project=False,
                        show_sources=True,
                        full=False,
                        show_shadowed=False,
                        issues_only=False,
                        limit=5,
                        format="json",
                    ),
                    root,
                    model,
                    "reuse",
                ),
                CLI.EXIT_OK,
            )
        self.assertEqual(emitted[-1], {"compact": True})

        with (
            mock.patch.object(CLI, "sync_runtime", return_value=["sync"]),
            mock.patch.object(CLI, "select_tasks", return_value=[{"id": "bootstrap"}]),
            mock.patch.object(CLI, "resolve_tasks_for_run", return_value=[{"id": "bootstrap"}]),
            mock.patch.object(CLI, "select_env_files_for_tasks", return_value=[]),
            mock.patch.object(CLI, "ensure_required_env_files_ready") as ensure_env,
            mock.patch.object(CLI, "run_tasks", return_value=[{"id": "bootstrap", "state": "ready"}]),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(
                CLI._handle_bootstrap(_ns(task=["bootstrap"], dry_run=False, format="json"), root, model, "reuse"),
                CLI.EXIT_OK,
            )
        ensure_env.assert_called_once()
        self.assertEqual(emitted[-1]["sync_actions"], ["sync"])

        with (
            mock.patch.object(CLI, "select_services", return_value=[{"id": "api"}]),
            mock.patch.object(
                CLI,
                "collect_service_logs",
                return_value=[{"id": "api", "log_file": "/logs/api.log", "present": True, "lines": ["ok"]}],
            ),
            redirect_stdout(StringIO()) as stdout,
        ):
            self.assertEqual(
                CLI._handle_logs(_ns(service=["api"], lines=10, format="text"), root, model, "reuse"),
                CLI.EXIT_OK,
            )
        self.assertIn("api", stdout.getvalue())

    def test_handle_up_covers_local_legacy_bridge_and_success_paths(self) -> None:
        emitted: list[dict[str, object]] = []
        root = Path("/tmp/skillbox")

        with (
            mock.patch.object(CLI, "local_runtime_active_profile", return_value="local-minimal"),
            mock.patch.object(CLI, "run_up", return_value=(CLI.EXIT_ERROR, {"error": {"message": "bad"}})),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            args = _ns(format="json", client=["personal"], service=["api"], dry_run=False, wait_seconds=1.0)
            self.assertEqual(CLI._handle_up(args, root, {}, "reuse"), CLI.EXIT_ERROR)
        self.assertEqual(emitted[-1]["error"]["message"], "bad")

        with (
            mock.patch.object(CLI, "local_runtime_active_profile", return_value=None),
            mock.patch.object(CLI, "select_services", return_value=[]),
            mock.patch.object(CLI, "bridge_outputs_state", return_value={"state": "missing"}),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            args = _ns(format="json", client=["personal"], profile=["core"], dry_run=False)
            self.assertEqual(CLI._handle_up(args, root, {"bridges": [{"id": "env"}]}, "reuse"), CLI.EXIT_ERROR)
        self.assertEqual(emitted[-1]["error"]["type"], "LOCAL_RUNTIME_ENV_BRIDGE_FAILED")

        with (
            mock.patch.object(CLI, "local_runtime_active_profile", return_value=None),
            mock.patch.object(CLI, "select_services", return_value=[{"id": "api"}]),
            mock.patch.object(CLI, "sync_runtime", return_value=["sync"]),
            mock.patch.object(CLI, "resolve_services_for_start", return_value=[{"id": "api"}]),
            mock.patch.object(CLI, "resolve_tasks_for_services", return_value=[{"id": "bootstrap"}]),
            mock.patch.object(CLI, "select_env_files_for_tasks", return_value=[]),
            mock.patch.object(CLI, "select_env_files_for_services", return_value=[]),
            mock.patch.object(CLI, "ensure_required_env_files_ready"),
            mock.patch.object(CLI, "run_tasks", return_value=[{"result": "ok"}]),
            mock.patch.object(CLI, "start_services", return_value=[{"result": "ok"}]),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            args = _ns(format="json", service=["api"], dry_run=False, wait_seconds=1.0)
            self.assertEqual(CLI._handle_up(args, root, {"bridges": []}, "reuse"), CLI.EXIT_OK)
        self.assertEqual(emitted[-1]["sync_actions"], ["sync"])

    def test_main_dispatches_mode_errors_model_handlers_and_catches_errors(self) -> None:
        emitted: list[dict[str, object]] = []

        def run_main(argv: list[str]) -> int:
            with mock.patch.object(sys, "argv", ["manage.py", *argv]):
                return CLI.main()

        with mock.patch.object(CLI, "emit_json", side_effect=emitted.append):
            self.assertEqual(run_main(["up", "--mode", "bad", "--format", "json"]), CLI.EXIT_ERROR)
        self.assertEqual(emitted[-1]["error"]["type"], "LOCAL_RUNTIME_MODE_UNSUPPORTED")

        def ok_handler(args: argparse.Namespace, root_dir: Path, model: dict[str, object], mode: str) -> int:
            self.assertEqual(mode, "reuse")
            self.assertEqual(model["filtered"], True)
            return CLI.EXIT_OK

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.dict(CLI._MODEL_DISPATCH, {"status": ok_handler}),
            mock.patch.object(CLI, "build_runtime_model", return_value={"raw": True}),
            mock.patch.object(CLI, "normalize_active_profiles", return_value=["core"]),
            mock.patch.object(CLI, "normalize_active_clients", return_value=["personal"]),
            mock.patch.object(CLI, "filter_model", return_value={"filtered": True}),
            mock.patch.object(CLI, "_check_logs_deferred_surfaces", return_value=None),
        ):
            self.assertEqual(run_main(["--root-dir", tmpdir, "status", "--format", "json"]), CLI.EXIT_OK)

        def runtime_error_handler(args: argparse.Namespace, root_dir: Path, model: dict[str, object], mode: str) -> int:
            raise RuntimeError("broken")

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.dict(CLI._MODEL_DISPATCH, {"status": runtime_error_handler}),
            mock.patch.object(CLI, "build_runtime_model", return_value={}),
            mock.patch.object(CLI, "normalize_active_profiles", return_value=[]),
            mock.patch.object(CLI, "normalize_active_clients", return_value=[]),
            mock.patch.object(CLI, "filter_model", return_value={}),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(run_main(["--root-dir", tmpdir, "status", "--format", "json"]), CLI.EXIT_ERROR)
        self.assertEqual(emitted[-1]["error"]["message"], "broken")

    def test_high_risk_handlers_emit_structured_payloads(self) -> None:
        emitted: list[dict[str, object]] = []
        root = Path("/tmp/skillbox")

        skill_args = _ns(
            skill_action="remove",
            skill_name="demo",
            cwd=None,
            to="project",
            category=[],
            source=None,
            from_scope="all",
            prune=False,
            force=False,
            allow_directories=False,
            yes=False,
        )
        with self.assertRaises(RuntimeError):
            CLI._handle_skill(skill_args, root, {}, "reuse")

        skill_args.skill_action = "plan"
        with (
            mock.patch.object(CLI, "skill_lifecycle_plan", return_value={"actions": []}),
            mock.patch.object(CLI, "apply_skill_lifecycle_plan", return_value={"actions": [{"status": "ok"}]}),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_skill(skill_args, root, {}, "reuse"), CLI.EXIT_OK)

        skill_args.skill_action = "sync"
        skill_args.yes = True
        skill_args.prune = True
        with (
            mock.patch.object(CLI, "skill_lifecycle_plan", return_value={"actions": []}),
            mock.patch.object(CLI, "apply_skill_lifecycle_plan", return_value={"actions": [{"status": "blocked"}]}),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_skill(skill_args, root, {}, "reuse"), CLI.EXIT_DRIFT)

        client_args = _ns(
            list_blueprints=True,
            client_id=None,
            set=[],
            label=None,
            default_cwd=None,
            root_path=None,
            blueprint=None,
            force=False,
        )
        with (
            mock.patch.object(CLI, "list_client_blueprints", return_value=[{"id": "service"}]),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_client_init(client_args, root), CLI.EXIT_OK)
        self.assertEqual(emitted[-1]["blueprints"], [{"id": "service"}])

        client_args.list_blueprints = False
        with mock.patch.object(CLI, "emit_json", side_effect=emitted.append):
            self.assertEqual(CLI._handle_client_init(client_args, root), CLI.EXIT_ERROR)
        self.assertEqual(emitted[-1]["error"]["type"], "missing_argument")

        client_args.client_id = "personal"
        with (
            mock.patch.object(CLI, "parse_key_value_assignments", return_value=[("SERVICE", "api")]),
            mock.patch.object(CLI, "scaffold_client_overlay", return_value=(["write-file: overlay"], {"id": "service"})),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_client_init(client_args, root), CLI.EXIT_OK)
        self.assertEqual(emitted[-1]["blueprint"], {"id": "service"})

    def test_session_distribution_and_runtime_handlers_cover_success_and_error_paths(self) -> None:
        emitted: list[dict[str, object]] = []
        root = Path("/tmp/skillbox")

        session_payload = {"client_id": "personal", "session": {"session_id": "s1", "status": "active", "recent_events": []}}
        with (
            mock.patch.object(CLI, "start_client_session", return_value=session_payload),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            args = _ns(client_id="personal", label="", cwd="", goal="", actor="")
            self.assertEqual(CLI._handle_session_start(args, root), CLI.EXIT_OK)
        with (
            mock.patch.object(CLI, "append_client_session_event", side_effect=RuntimeError("Session not found: s1")),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            args = _ns(client_id="personal", session_id="s1", event_type="note", message="", actor="")
            self.assertEqual(CLI._handle_session_event(args, root), CLI.EXIT_ERROR)
        with (
            mock.patch.object(CLI, "session_status_payload", return_value=session_payload),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            args = _ns(client_id="personal", session_id="s1", limit=10)
            self.assertEqual(CLI._handle_session_status(args, root), CLI.EXIT_OK)
        with (
            mock.patch.object(CLI, "session_status_payload", return_value={"client_id": "personal", "sessions": [session_payload["session"]], "count": 1}),
            redirect_stdout(StringIO()),
        ):
            args = _ns(format="text", client_id="personal", session_id=None, limit=10)
            self.assertEqual(CLI._handle_session_status(args, root), CLI.EXIT_OK)

        self.assertEqual(CLI._parse_distribution_pin_args(["deploy=2"]), {"deploy": 2})
        for pin in ("deploy", "=2", "deploy=bad"):
            with self.subTest(pin=pin):
                with self.assertRaises(CLI.DistributionPreviewError):
                    CLI._parse_distribution_pin_args([pin])

        rollback_args = _ns(
            list=True,
            skill="deploy",
            state_root="state",
            manifest_path="manifest.json",
            public_key="pub",
            distributor_id="dist",
            version=1,
            install_target=[],
            lockfile="lock.json",
            reason="test",
            emergency_override=False,
        )
        with (
            mock.patch.object(CLI, "host_path_to_absolute_path", side_effect=lambda root_dir, value: root_dir / value),
            mock.patch.object(CLI, "cached_versions", return_value=[1, 2]),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_distribution_rollback(rollback_args, root), CLI.EXIT_OK)
        rollback_args.list = False
        with (
            mock.patch.object(CLI, "host_path_to_absolute_path", side_effect=lambda root_dir, value: root_dir / value),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_distribution_rollback(rollback_args, root), CLI.EXIT_ERROR)

        preview_args = _ns(
            manifest_path="manifest.json",
            public_key="pub",
            distributor_id="dist",
            state_root="state",
            pick=[],
            pin=[],
            target_env="codex",
            lockfile=None,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            with (
                mock.patch.object(CLI, "host_path_to_absolute_path", side_effect=lambda root_dir, value: manifest if value == "manifest.json" else root_dir / value),
                mock.patch.object(CLI, "preview_manifest", return_value={"ready": False, "distributor_id": "dist", "manifest_version": 1, "items": []}),
                mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
            ):
                self.assertEqual(CLI._handle_distribution_preview(preview_args, root), CLI.EXIT_ERROR)

        with (
            mock.patch.object(CLI, "select_services", return_value=[{"id": "api"}]),
            mock.patch.object(CLI, "resolve_services_for_stop", return_value=[{"id": "api"}]),
            mock.patch.object(CLI, "stop_services", return_value=[{"result": "ok"}]),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            args = _ns(service=["api"], dry_run=True, wait_seconds=0)
            self.assertEqual(CLI._handle_down(args, root, {}, "reuse"), CLI.EXIT_OK)

        with (
            mock.patch.object(CLI, "doctor_results", return_value=[CLI.CheckResult("fail", "api", "down")]),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            args = _ns(format="json")
            self.assertEqual(CLI._handle_doctor(args, root, {}, "reuse"), CLI.EXIT_DRIFT)

        with (
            mock.patch.object(CLI, "select_services", return_value=[{"id": "api"}]),
            mock.patch.object(CLI, "collect_service_logs", return_value=[{"id": "api", "lines": []}]),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            args = _ns(service=["api"], lines=20)
            self.assertEqual(CLI._handle_logs(args, root, {}, "reuse"), CLI.EXIT_OK)

    def test_remaining_cli_handlers_cover_publish_mmdx_focus_restart_and_deferred_logs(self) -> None:
        emitted: list[dict[str, object]] = []
        root = Path("/tmp/skillbox")

        publish_payload = {
            "client_id": "personal",
            "target_dir": "/tmp/private",
            "changed": True,
            "payload_tree_sha256": "abc",
            "acceptance": {"present": True, "accepted_at": "now", "active_profiles": ["core"]},
            "deploy": {"present": True, "manifest": "deploy.json", "archive": "deploy.zip"},
            "commit_hash": "abc123",
            "actions": ["copy"],
        }
        publish_args = _ns(
            client_id="personal",
            target_dir=None,
            from_bundle=None,
            profile=[],
            acceptance=True,
            deploy_artifact=True,
            commit=True,
        )
        with (
            mock.patch.object(CLI, "publish_client_bundle", return_value=publish_payload),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_client_publish(publish_args, root), CLI.EXIT_OK)
        with (
            mock.patch.object(CLI, "publish_client_bundle", side_effect=RuntimeError("target repo has a dirty working tree")),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_client_publish(publish_args, root), CLI.EXIT_ERROR)

        mmdx_args = _ns(cwd=None, query=["graph"], search_root=[], open=True, limit=3, tmux=False, tmux_submit=False, allow_parser_install=False)
        with (
            mock.patch.object(CLI, "mmdx_open_payload", return_value=({"ok": True}, CLI.EXIT_OK)),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_mmdx(mmdx_args, root), CLI.EXIT_OK)
        with (
            mock.patch.object(CLI, "mmdx_open_payload", side_effect=RuntimeError("bad mmdx")),
            mock.patch.object(CLI, "mmdx_error_payload", return_value={"error": {"message": "bad mmdx"}}),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_mmdx(mmdx_args, root), CLI.EXIT_ERROR)

        booking_args = _ns(
            action="availability",
            client=["personal"],
            date=None,
            slot=None,
            email=None,
            name=None,
            redirect_url=None,
            origin=None,
            send_magic_link=False,
            dry_run=False,
            limit=8,
            access_token_env=None,
        )
        with (
            mock.patch.object(CLI, "operator_booking_payload", return_value=({"action": "availability"}, CLI.EXIT_OK)),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_operator_booking(booking_args, root, {}, "reuse"), CLI.EXIT_OK)
        with (
            mock.patch.object(CLI, "operator_booking_payload", side_effect=RuntimeError("booking unavailable")),
            mock.patch.object(CLI, "operator_booking_error_payload", return_value={"error": {"message": "booking unavailable"}}),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_operator_booking(booking_args, root, {}, "reuse"), CLI.EXIT_ERROR)

        with redirect_stderr(StringIO()):
            focus_args = _ns(format="text", client_id="", client=[], resume=False)
            self.assertEqual(CLI._handle_focus(focus_args, root), CLI.EXIT_ERROR)
        with (
            mock.patch.object(CLI, "run_focus", return_value=CLI.EXIT_OK),
            mock.patch.object(CLI, "resolve_context_dir", return_value=root),
        ):
            focus_args = _ns(client_id="", client=["personal"], profile=["core"], service=["api"], resume=False, context_dir=None, wait_seconds=1.0)
            self.assertEqual(CLI._handle_focus(focus_args, root), CLI.EXIT_OK)

        with redirect_stderr(StringIO()):
            report_args = _ns(format="text", client_id="", client=[])
            self.assertEqual(CLI._handle_stewardship_report(report_args, root), CLI.EXIT_ERROR)
        with mock.patch.object(CLI, "run_stewardship_report", return_value=CLI.EXIT_OK):
            report_args = _ns(client_id="", client=["personal"], profile=[], write=False, output_dir=None)
            self.assertEqual(CLI._handle_stewardship_report(report_args, root), CLI.EXIT_OK)

        logs_args = _ns(command="logs", service=["api"], client=["personal"], profile=["core"])
        with (
            mock.patch.object(CLI, "classify_requested_surfaces", return_value={"deferred": [("api", {"id": "api"})]}),
            mock.patch.object(CLI, "build_local_runtime_service_deferred_error", return_value={"error": {"type": "deferred"}}),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._check_logs_deferred_surfaces(logs_args, {}, ["api"]), CLI.EXIT_ERROR)
        with mock.patch.object(CLI, "classify_requested_surfaces", return_value={"deferred": []}):
            self.assertIsNone(CLI._check_logs_deferred_surfaces(logs_args, {}, ["api"]))
        self.assertIsNone(CLI._check_logs_deferred_surfaces(_ns(command="status", service=[]), {}, []))

        restart_args = _ns(service=["api"], dry_run=False, wait_seconds=1.0)
        with (
            mock.patch.object(CLI, "select_services", return_value=[{"id": "api"}]),
            mock.patch.object(CLI, "resolve_services_for_stop", return_value=[{"id": "api"}]),
            mock.patch.object(CLI, "resolve_services_for_start", return_value=[{"id": "api"}]),
            mock.patch.object(CLI, "resolve_tasks_for_services", return_value=[{"id": "bootstrap"}]),
            mock.patch.object(CLI, "stop_services", return_value=[{"result": "ok"}]),
            mock.patch.object(CLI, "sync_runtime", return_value=["sync"]),
            mock.patch.object(CLI, "select_env_files_for_tasks", return_value=[]),
            mock.patch.object(CLI, "select_env_files_for_services", return_value=[]),
            mock.patch.object(CLI, "ensure_required_env_files_ready"),
            mock.patch.object(CLI, "run_tasks", return_value=[{"result": "ok"}]),
            mock.patch.object(CLI, "start_services", return_value=[{"result": "ok"}]),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_restart(restart_args, root, {}, "reuse"), CLI.EXIT_OK)


if __name__ == "__main__":
    unittest.main()
