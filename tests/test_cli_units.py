from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
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
from runtime_manager.command_registry import default_registry  # noqa: E402
from runtime_manager.errors import PRUNE_SKIPPED_PINNED  # noqa: E402


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


def _run_manage(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, ".env-manager/manage.py", *args],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
    )


def _load_mcp_server_module():
    module_path = ROOT_DIR / ".env-manager" / "mcp_server.py"
    spec = importlib.util.spec_from_file_location("skillbox_mcp_server_for_tests", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CliUnitTests(unittest.TestCase):
    def test_focus_json_missing_client_returns_structured_error(self) -> None:
        args = _ns(client_id="", resume=False, context_dir=None)
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = CLI._handle_focus(args, Path("/repo"))  # noqa: SLF001

        self.assertEqual(exit_code, CLI.EXIT_ERROR)
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["error"]["type"], "runtime_error")
        self.assertIn("focus requires a client_id or --resume", payload["error"]["message"])

    def test_capabilities_json_contract_is_agent_readable(self) -> None:
        result = _run_manage("capabilities", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["tool"], "skillbox-manage")
        self.assertEqual(payload["contract_version"], "2026-05-09")
        self.assertIn("capabilities", payload["agent_surfaces"])
        self.assertTrue(any(command["name"] == "next" for command in payload["commands"]))
        self.assertTrue(any(command["name"] == "graph" for command in payload["commands"]))
        self.assertTrue(any(command["name"] == "explain" for command in payload["commands"]))
        self.assertTrue(any(command["name"] == "search" for command in payload["commands"]))
        self.assertTrue(any(command["name"] == "snap" for command in payload["commands"]))
        self.assertIn("--json", payload["agent_surfaces"]["json_aliases"])
        self.assertTrue(any(command["name"] == "status" for command in payload["commands"]))
        self.assertIn("registry", payload)
        self.assertEqual(payload["registry"]["abi_version"], "2026-06-11+agent_ops_brain")
        self.assertGreaterEqual(payload["registry"]["counts"]["tier1"], 6)
        registry_entries = {entry["id"]: entry for entry in payload["registry"]["capabilities"]}
        self.assertIn("brain.next", registry_entries)
        next_entry = registry_entries["brain.next"]
        self.assertEqual(next_entry["risk"], "low")
        self.assertEqual(next_entry["side_effect"], "none")
        self.assertIn("inputs", next_entry)
        self.assertIn("outputs", next_entry)
        self.assertIn("examples", next_entry)
        launch = next(command for command in payload["commands"] if command["name"] == "swimmers-launch")
        self.assertIn("--dry-run", launch["safe_first_try"])
        robot_docs = next(command for command in payload["commands"] if command["name"] == "robot-docs")
        self.assertTrue(robot_docs["json"])
        self.assertIn("python3 scripts/04-reconcile.py capabilities --json", payload["agent_surfaces"]["outer_reconcile"])
        self.assertIn(
            "python3 .env-manager/manage.py distribution-preview --manifest-path <manifest.json> --public-key <public-key.pem> --format json",
            payload["safe_previews"],
        )
        client_project = next(command for command in payload["commands"] if command["name"] == "client-project")
        self.assertIn("--dry-run", client_project["safe_first_try"])
        parity_report = next(command for command in payload["commands"] if command["name"] == "parity-report")
        self.assertIn("parity-report <client> --format json", parity_report["safe_first_try"])
        pressure_report = next(command for command in payload["commands"] if command["name"] == "pressure-report")
        self.assertIn("pressure-report --format json", pressure_report["safe_first_try"])
        rch_report = next(command for command in payload["commands"] if command["name"] == "rch-report")
        self.assertIn("rch-report --format json", rch_report["safe_first_try"])
        rch_stage = next(command for command in payload["commands"] if command["name"] == "rch-stage")
        self.assertIn("rch-stage --dry-run --format json", rch_stage["safe_first_try"])
        sbh_report = next(command for command in payload["commands"] if command["name"] == "sbh-report")
        self.assertIn("sbh-report --format json", sbh_report["safe_first_try"])
        next_command = next(command for command in payload["commands"] if command["name"] == "next")
        self.assertIn("--no-adapters", next_command["safe_first_try"])
        search_command = next(command for command in payload["commands"] if command["name"] == "search")
        self.assertEqual(search_command["safe_first_try"], "manage.py search graph --format json --no-adapters")

    def test_capabilities_compact_registry_is_parser_backed(self) -> None:
        result = _run_manage("capabilities", "--compact", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["contract_version"], "2026-05-09")
        registry_entries = {entry["id"]: entry for entry in payload["registry"]["capabilities"]}
        next_entry = registry_entries["brain.next"]
        self.assertEqual(next_entry["risk"], "low")
        self.assertEqual(next_entry["side_effect"], "none")
        self.assertIn("summary", next_entry)
        self.assertNotIn("inputs", next_entry)
        self.assertNotIn("outputs", next_entry)
        self.assertNotIn("examples", next_entry)

    def test_robot_docs_guide_is_available_in_tool(self) -> None:
        result = _run_manage("robot-docs", "guide")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Skillbox agent guide", result.stdout)
        self.assertIn("capabilities --json", result.stdout)
        self.assertIn("Safe mutation pattern", result.stdout)
        self.assertIn("parity-report <client>", result.stdout)
        self.assertIn("pressure-report --format json", result.stdout)
        self.assertIn("rch-report --format json", result.stdout)
        self.assertIn("rch-stage --dry-run", result.stdout)
        self.assertIn("sbh-report --format json", result.stdout)
        self.assertIn("worker-devbox", result.stdout)
        self.assertIn("primary-prod", result.stdout)
        self.assertIn("Protected paths", result.stdout)
        self.assertIn("swimmers-launch <dirs...>", result.stdout)
        self.assertIn("next --format json", result.stdout)
        self.assertIn("graph --format json", result.stdout)

    def test_agent_ops_brain_cli_surfaces_emit_json(self) -> None:
        commands = [
            ("graph", "--algorithm", "critical-path", "--format", "json", "--no-adapters"),
            ("next", "--format", "json", "--no-adapters", "--limit", "1"),
            ("explain", "brain.next", "--format", "json", "--no-adapters"),
            ("search", "graph", "--format", "json", "--no-adapters", "--limit", "1"),
            ("snap", "replay", "tests/goldens/agent_ops_snapshot.json", "--format", "json"),
        ]
        payloads = []
        for command in commands:
            with self.subTest(command=command[0]):
                result = _run_manage(*command)
                self.assertEqual(result.returncode, 0, result.stderr)
                payloads.append(json.loads(result.stdout))

        self.assertEqual(payloads[0]["algorithm"]["name"], "critical-path")
        self.assertTrue(payloads[1]["recommendations"])
        self.assertEqual(payloads[2]["kind"], "command")
        self.assertTrue(payloads[3]["hits"])
        self.assertEqual(payloads[4]["snapshot_id"], "golden-fixture")

    def test_explain_bare_brain_command_alias_resolves_to_command_node(self) -> None:
        result = _run_manage("explain", "next", "--format", "json", "--no-adapters")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["target"], "command:brain.next")
        self.assertEqual(payload["kind"], "command")

    def test_snap_without_action_returns_structured_usage_payload(self) -> None:
        result = _run_manage("snap", "--format", "json")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "SNAP_ACTION_REQUIRED")
        self.assertEqual([action["name"] for action in payload["actions"]], ["create", "diff", "replay"])
        self.assertIn("snap replay tests/goldens/agent_ops_snapshot.json", payload["next_actions"][0])

    def test_graph_invalid_algorithm_returns_structured_json_error(self) -> None:
        result = _run_manage("graph", "--algorithm", "pagerank", "--format", "json", "--no-adapters")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")
        self.assertIn("allowed", payload["error"]["details"])
        self.assertTrue(all(action.startswith("python3 .env-manager/manage.py ") for action in payload["next_actions"]))

    def test_agent_ops_brain_text_renderers_cover_success_and_errors(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            CLI._print_next_text(
                {
                    "summary": {"returned": 1, "recommendation_count": 2},
                    "recommendations": [
                        {
                            "id": "claim-ready:one",
                            "score": 10,
                            "risk": "low",
                            "side_effect": "none",
                            "reasons": ["BR reports ready work"],
                            "commands": ["br update one --status=in_progress"],
                        }
                    ],
                    "disagreements": [{"code": "BV_BR_DISAGREE", "message": "bv and br differ"}],
                }
            )
            CLI._print_explain_text(
                {
                    "target": "command:brain.next",
                    "kind": "command",
                    "summary": "Rank work",
                    "relationships": {"incoming_count": 1, "outgoing_count": 2},
                    "commands": [{"id": "brain.next", "summary": "Rank next actions"}],
                }
            )
            CLI._print_search_text(
                {
                    "query": "graph",
                    "count": 1,
                    "total_count": 2,
                    "hits": [
                        {
                            "source": "registry",
                            "kind": "command",
                            "id": "brain.graph",
                            "score": 7,
                            "snippet": "Inspect graph",
                            "next_action": "explain brain.graph",
                        }
                    ],
                    "warnings": [{"code": "MISSING_DOC", "message": "doc unavailable"}],
                }
            )
            CLI._print_snap_text({"snapshot_id": "abc", "label": "fixture", "inputs": {}, "artifact": "/tmp/abc.json"})
            CLI._print_snap_text(
                {
                    "change_count": 1,
                    "changes": [{"severity": "high", "change": "modified", "entity": "doctor.check:runtime"}],
                }
            )
            CLI._print_snap_text({"snapshot_id": "abc", "summary": {"services": 1, "graph_nodes": 2}})
            CLI._print_explain_text({"error": {"message": "missing node"}})
            CLI._print_search_text({"error": {"message": "empty query"}})
            CLI._print_snap_text({"error": {"message": "bad snapshot"}})

        rendered = stdout.getvalue()
        self.assertIn("next: 1/2 recommendations", rendered)
        self.assertIn("BV_BR_DISAGREE", rendered)
        self.assertIn("explain: command:brain.next", rendered)
        self.assertIn("search: 1/2 hits", rendered)
        self.assertIn("snapshot: abc", rendered)
        self.assertIn("snapshot diff: 1 changes", rendered)
        self.assertIn("snapshot replay: abc services=1 graph_nodes=2", rendered)
        self.assertIn("missing node", stderr.getvalue())
        self.assertIn("empty query", stderr.getvalue())
        self.assertIn("bad snapshot", stderr.getvalue())

    def test_agent_ops_brain_handlers_cover_direct_text_json_and_snap_branches(self) -> None:
        emitted: list[dict[str, object]] = []
        graph_payload = {"nodes": [], "edges": [], "warnings": []}
        recommendation_payload = {
            "summary": {"returned": 0, "recommendation_count": 0},
            "recommendations": [],
            "disagreements": [],
        }
        search_result = {
            "query": "graph",
            "count": 0,
            "total_count": 0,
            "hits": [],
            "warnings": [],
        }

        class TinyGraph:
            def to_payload(self) -> dict[str, object]:
                return graph_payload

        before = CLI.create_snapshot_payload(created_at="2026-06-11T00:00:00Z")
        after = CLI.create_snapshot_payload(
            status={"services": [{"id": "api", "state": "down"}]},
            created_at="2026-06-11T00:01:00Z",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before_path = root / "before.json"
            after_path = root / "after.json"
            before_path.write_text(json.dumps(before), encoding="utf-8")
            after_path.write_text(json.dumps(after), encoding="utf-8")

            with (
                mock.patch.object(CLI, "_brain_adapters_for_args", return_value={"evidence": {"payload": {}}}),
                mock.patch.object(CLI, "_brain_graph_payload", return_value=graph_payload),
                mock.patch.object(CLI, "next_action_payload", return_value=recommendation_payload),
                mock.patch.object(CLI, "graph_command_payload", return_value={"ok": True, "graph": graph_payload}),
                mock.patch.object(CLI, "render_graph_payload", return_value="graph text"),
                mock.patch.object(CLI, "explain_payload", return_value={"error": {"message": "missing node"}}),
                mock.patch.object(CLI, "search_payload", return_value=search_result) as search_mock,
                mock.patch.object(CLI, "runtime_status", return_value={"services": []}),
                mock.patch.object(CLI, "doctor_results", return_value=[]),
                mock.patch.object(CLI, "collect_runtime_evidence", return_value={"overall": "green"}),
                mock.patch.object(CLI, "_full_declared_mcp_servers", return_value=[]),
                mock.patch.object(CLI, "build_agent_graph", return_value=TinyGraph()),
                mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
                redirect_stdout(StringIO()) as stdout,
                redirect_stderr(StringIO()) as stderr,
            ):
                self.assertEqual(
                    CLI._handle_next(_ns(format="text", limit=2, no_adapters=False), root, {}, "reuse"),
                    CLI.EXIT_OK,
                )
                self.assertEqual(
                    CLI._handle_graph(
                        _ns(format="text", algorithm=None, node=None, source=None, target=None, blocked_node=[]),
                        root,
                        {},
                        "reuse",
                    ),
                    CLI.EXIT_OK,
                )
                self.assertEqual(
                    CLI._handle_explain(_ns(format="text", target="missing"), root, {}, "reuse"),
                    CLI.EXIT_ERROR,
                )
                self.assertEqual(
                    CLI._handle_search(
                        _ns(
                            format="json",
                            query=["graph"],
                            source_filter=["registry"],
                            kind_filter=["command"],
                            limit=3,
                        ),
                        root,
                        {},
                        "reuse",
                    ),
                    CLI.EXIT_OK,
                )
                self.assertEqual(
                    CLI._handle_snap(
                        _ns(
                            format="text",
                            snap_action="create",
                            name="fixture",
                            created_at="2026-06-11T00:00:00Z",
                            write=True,
                            cwd=None,
                            ntm_session=None,
                            no_adapters=False,
                        ),
                        root,
                        {},
                        "reuse",
                    ),
                    CLI.EXIT_OK,
                )
                self.assertEqual(
                    CLI._handle_snap(
                        _ns(format="text", snap_action="diff", paths=[], from_path=str(before_path), to_path=str(after_path)),
                        root,
                        {},
                        "reuse",
                    ),
                    CLI.EXIT_OK,
                )
                self.assertEqual(
                    CLI._handle_snap(
                        _ns(format="json", snap_action="replay", path=str(before_path)),
                        root,
                        {},
                        "reuse",
                    ),
                    CLI.EXIT_OK,
                )
                self.assertEqual(
                    CLI._handle_snap(_ns(format="text", snap_action="unknown"), root, {}, "reuse"),
                    CLI.EXIT_ERROR,
                )

        self.assertTrue(emitted)
        search_mock.assert_called_once()
        self.assertIn("graph text", stdout.getvalue())
        self.assertIn("missing node", stderr.getvalue())
        self.assertIn("unknown snap action", stderr.getvalue())

    def test_agent_ops_brain_mcp_tools_are_declared_and_routed_once(self) -> None:
        mcp_server = _load_mcp_server_module()
        tool_names = {tool["name"] for tool in mcp_server.TOOLS}
        dispatch_names = set(mcp_server._DISPATCH)  # noqa: SLF001
        registry_mcp_tools = {
            spec.mcp_tool
            for spec in default_registry()
            if spec.id in {
                "runtime.capabilities",
                "brain.next",
                "brain.graph",
                "brain.explain",
                "brain.search",
                "brain.snap",
            }
        }

        self.assertEqual(
            registry_mcp_tools,
            {
                "skillbox_capabilities",
                "skillbox_next",
                "skillbox_graph",
                "skillbox_explain",
                "skillbox_search",
                "skillbox_snap",
            },
        )
        self.assertTrue(registry_mcp_tools <= tool_names)
        self.assertTrue(registry_mcp_tools <= dispatch_names)
        self.assertEqual(mcp_server._DISPATCH["skillbox_next"], ("next", None))  # noqa: SLF001
        self.assertEqual(mcp_server._DISPATCH["skillbox_snap"], ("snap", "action"))  # noqa: SLF001

        search_args = mcp_server.build_args("search", {"query": "graph", "no_adapters": True})
        self.assertEqual(search_args, ["search", "--format", "json", "graph", "--no-adapters"])
        snap_args = mcp_server.build_args(
            "snap",
            {"action": "replay", "path": "tests/goldens/agent_ops_snapshot.json"},
            "replay",
        )
        self.assertEqual(
            snap_args,
            ["snap", "replay", "--format", "json", "tests/goldens/agent_ops_snapshot.json"],
        )

    def test_agent_ops_brain_mcp_dispatch_matches_cli_representatives(self) -> None:
        mcp_server = _load_mcp_server_module()

        cli_snap = json.loads(
            _run_manage("snap", "replay", "tests/goldens/agent_ops_snapshot.json", "--format", "json").stdout
        )
        mcp_snap = mcp_server.dispatch_tool(
            "skillbox_snap",
            {"action": "replay", "path": "tests/goldens/agent_ops_snapshot.json"},
        )
        mcp_snap_payload = json.loads(mcp_snap["content"][0]["text"])

        self.assertNotIn("isError", mcp_snap)
        self.assertEqual(mcp_snap_payload["_exit_code"], 0)
        self.assertEqual(mcp_snap_payload["snapshot_id"], cli_snap["snapshot_id"])
        self.assertEqual(mcp_snap_payload["summary"], cli_snap["summary"])

        cli_search = json.loads(
            _run_manage("search", "graph", "--format", "json", "--no-adapters", "--limit", "1").stdout
        )
        mcp_search = mcp_server.dispatch_tool(
            "skillbox_search",
            {"query": "graph", "no_adapters": True, "limit": 1},
        )
        mcp_search_payload = json.loads(mcp_search["content"][0]["text"])

        self.assertNotIn("isError", mcp_search)
        self.assertEqual(mcp_search_payload["_exit_code"], 0)
        self.assertEqual(mcp_search_payload["hits"][0]["id"], cli_search["hits"][0]["id"])
        self.assertEqual(mcp_search_payload["hits"][0]["score"], cli_search["hits"][0]["score"])

    def test_swimmers_launch_cli_dry_run_resolves_against_invoke_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            invoke_cwd = Path(tmpdir) / "repo"
            invoke_cwd.mkdir()
            result = subprocess.run(
                [
                    sys.executable,
                    ".env-manager/manage.py",
                    "swimmers-launch",
                    "core",
                    "../api",
                    "--invoke-cwd",
                    str(invoke_cwd),
                    "--request",
                    "Audit auth drift",
                    "--dry-run",
                    "--format",
                    "json",
                ],
                cwd=ROOT_DIR,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(
            payload["request_body"],
            {
                "dirs": [str(invoke_cwd / "core"), str(invoke_cwd.parent / "api")],
                "spawn_tool": "codex",
                "initial_request": "Audit auth drift",
            },
        )

    def test_json_typo_alias_keeps_stdout_parseable_and_warns_on_stderr(self) -> None:
        result = subprocess.run(
            [sys.executable, ".env-manager/manage.py", "status", "--jsno"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("active_profiles", payload)
        self.assertIn("Interpreting --jsno as --format json", result.stderr)

    def test_unknown_command_error_suggests_correct_command_and_capabilities(self) -> None:
        result = subprocess.run(
            [sys.executable, ".env-manager/manage.py", "statu", "--json"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("Did you mean: `manage.py status`?", result.stderr)
        self.assertIn("manage.py capabilities --json", result.stderr)

    def test_cli_import_does_not_require_distribution_crypto_modules(self) -> None:
        code = r"""
import builtins
import sys

sys.path.insert(0, ".env-manager")
real_import = builtins.__import__

def blocked_import(name, *args, **kwargs):
    if name == "cryptography" or name.startswith("cryptography."):
        raise ModuleNotFoundError("blocked cryptography import")
    return real_import(name, *args, **kwargs)

builtins.__import__ = blocked_import
import runtime_manager.cli
print("ok")
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_distribution_lazy_import_reports_missing_crypto(self) -> None:
        code = r"""
import builtins
import sys

sys.path.insert(0, ".env-manager")
real_import = builtins.__import__

def blocked_import(name, *args, **kwargs):
    if name == "cryptography" or name.startswith("cryptography."):
        raise ModuleNotFoundError("blocked cryptography import", name="cryptography")
    return real_import(name, *args, **kwargs)

import runtime_manager.cli as cli
builtins.__import__ = blocked_import
try:
    cli.publish_skill_release()
except RuntimeError as exc:
    print(str(exc))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("requires the optional 'cryptography' package", result.stdout)

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

        dry_run_text = StringIO()
        with redirect_stdout(dry_run_text):
            CLI._print_overlay_text(
                {
                    "action": "activate",
                    "name": "marketing",
                    "overlays": [],
                    "dry_run": True,
                    "unlinked": [],
                    "activations": [
                        {"skill": "demo", "activation_packet": {"name": "demo", "source": "overlay", "skill_md_sha256": "abc", "skill_md": "# Demo\n"}},
                    ],
                }
            )
        self.assertIn("overlay marketing: would activate", dry_run_text.getvalue())
        self.assertIn("would activate: 1 skills", dry_run_text.getvalue())
        self.assertNotIn("activated: 1 skills", dry_run_text.getvalue())

    def test_overlay_dry_run_previews_without_persisting_or_unlinking(self) -> None:
        emitted: list[dict[str, object]] = []

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(CLI, "active_overlays", return_value={"marketing"}),
            mock.patch.object(CLI, "set_overlay") as set_overlay,
            mock.patch.object(CLI, "toggle_overlay") as toggle_overlay,
            mock.patch.object(CLI, "unlink_overlay_scoped_skills") as unlink_overlay_scoped_skills,
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            root = Path(tmpdir)
            off_args = _ns(
                action="off",
                name="marketing",
                cwd=str(root),
                keep=False,
                to="project",
                scope="project",
                dry_run=True,
            )

            self.assertEqual(CLI._handle_overlay(off_args, root, {}, "reuse"), CLI.EXIT_OK)

        set_overlay.assert_not_called()
        toggle_overlay.assert_not_called()
        unlink_overlay_scoped_skills.assert_not_called()
        self.assertEqual(emitted[0]["overlays"], [])
        self.assertFalse(emitted[0]["persistent"])
        self.assertTrue(emitted[0]["would_persist"])
        self.assertEqual(emitted[0]["unlinked"], [])

    def test_overlay_no_args_lists_declared_overlays_with_on_off_state(self) -> None:
        # repos-sbp-overlay-semantics-vq0.3: `overlay` (no args) is the one-command
        # mode-discovery surface -- it lists the DECLARED registry annotated with
        # each overlay's live on/off state (not just the on-set).
        emitted: list[dict[str, object]] = []
        declared = [
            {"name": "marketing", "description": "GTM mode", "default_off": True},
            {"name": "research", "description": "", "default_off": True},
        ]
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(CLI, "declared_overlay_records", return_value=declared),
            mock.patch.object(CLI, "active_overlays", return_value={"marketing"}),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            root = Path(tmpdir)
            list_args = _ns(action="list", name="", cwd=str(root))
            self.assertEqual(CLI._handle_overlay(list_args, root, {}, "reuse"), CLI.EXIT_OK)

        payload = emitted[0]
        states = {row["name"]: row["state"] for row in payload["declared"]}
        self.assertEqual(states, {"marketing": "on", "research": "off"})
        self.assertEqual(payload["overlays"], ["marketing"])

        # And the text renderer prints declared rows + per-overlay state.
        text = StringIO()
        with redirect_stdout(text):
            CLI._print_overlay_text(payload)
        rendered = text.getvalue()
        self.assertIn("declared overlays:", rendered)
        self.assertIn("marketing: on", rendered)
        self.assertIn("research: off", rendered)

    def test_overlay_list_warns_on_undeclared_active_overlay(self) -> None:
        emitted: list[dict[str, object]] = []
        declared = [{"name": "marketing", "description": "", "default_off": True}]
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(CLI, "declared_overlay_records", return_value=declared),
            # "marketng" is a ghost typo present in overlay state.
            mock.patch.object(CLI, "active_overlays", return_value={"marketing", "marketng"}),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            root = Path(tmpdir)
            self.assertEqual(
                CLI._handle_overlay(_ns(action="list", name="", cwd=str(root)), root, {}, "reuse"),
                CLI.EXIT_OK,
            )
        self.assertEqual(emitted[0]["undeclared_active"], ["marketng"])
        text = StringIO()
        with redirect_stdout(text):
            CLI._print_overlay_text(emitted[0])
        self.assertIn("WARNING", text.getvalue())
        self.assertIn("marketng", text.getvalue())

    def test_overlay_on_undeclared_name_fails_with_declared_list(self) -> None:
        # An on/activate/toggle/off of an UNDECLARED overlay raises (EXIT_ERROR via
        # the top-level handler) BEFORE any state write, printing the declared list.
        declared = [{"name": "marketing", "description": "", "default_off": True}]
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(CLI, "declared_overlay_records", return_value=declared),
            mock.patch.object(CLI, "active_overlays", return_value=set()),
            mock.patch.object(CLI, "set_overlay") as set_overlay,
            mock.patch.object(CLI, "activate_overlay_scoped_skills") as activate,
        ):
            root = Path(tmpdir)
            for action in ("on", "activate", "toggle", "off"):
                args = _ns(
                    action=action, name="marketng", cwd=str(root), keep=False,
                    to="project", scope="project", category=[], source=None,
                )
                with self.assertRaises(RuntimeError) as cm:
                    CLI._handle_overlay(args, root, {}, "reuse")
                msg = str(cm.exception)
                self.assertIn("marketng", msg)
                self.assertIn("not a declared overlay", msg)
                self.assertIn("marketing", msg)  # the declared registry is printed
        # No state was written and no activation was attempted for the ghost name.
        set_overlay.assert_not_called()
        activate.assert_not_called()

    def test_overlay_on_declared_name_is_not_blocked_by_the_guard(self) -> None:
        declared = [{"name": "marketing", "description": "", "default_off": True}]
        state: set[str] = set()
        emitted: list[dict[str, object]] = []
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(CLI, "declared_overlay_records", return_value=declared),
            mock.patch.object(CLI, "active_overlays", side_effect=lambda: set(state)),
            mock.patch.object(CLI, "set_overlay", side_effect=lambda n, e: state.add(n) if e else state.discard(n)),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            root = Path(tmpdir)
            on_args = _ns(action="on", name="marketing", cwd=str(root), keep=False, to="project", scope="project")
            self.assertEqual(CLI._handle_overlay(on_args, root, {}, "reuse"), CLI.EXIT_OK)
        self.assertEqual(emitted[0]["overlays"], ["marketing"])

    def test_overlay_guard_is_noop_when_no_registry_declared(self) -> None:
        # The empty-model / no-registry path must NOT block on/off (legacy boxes
        # and the existing `{}`-model tests rely on this pass-through).
        state: set[str] = set()
        emitted: list[dict[str, object]] = []
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.object(CLI, "declared_overlay_records", return_value=[]),
            mock.patch.object(CLI, "active_overlays", side_effect=lambda: set(state)),
            mock.patch.object(CLI, "set_overlay", side_effect=lambda n, e: state.add(n) if e else state.discard(n)),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            root = Path(tmpdir)
            on_args = _ns(action="on", name="anything", cwd=str(root), keep=False, to="project", scope="project")
            self.assertEqual(CLI._handle_overlay(on_args, root, {}, "reuse"), CLI.EXIT_OK)
        self.assertEqual(emitted[0]["overlays"], ["anything"])

    def _write_overlay_policy_fixture(self, base: Path, skill_names: list[str]) -> tuple[dict[str, object], Path, Path]:
        """Scaffold a real skill-scope policy with a path-scoped marketing overlay.

        Returns (model, marketing_cwd, other_cwd). The marketing overlay rule is
        scoped to marketing_cwd only, so policy evaluation in other_cwd yields an
        empty wanted set even though every name is a literal overlay-tagged skill.
        """
        clients_root = base / "config" / "clients"
        clients_root.mkdir(parents=True)
        config_root = clients_root.parent
        skills_root = base / "skills"
        skills_root.mkdir()
        for name in skill_names:
            skill_dir = skills_root / name
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(f"# {name}\nbody\n", encoding="utf-8")
        marketing_cwd = base / "marketing_repo"
        marketing_cwd.mkdir()
        other_cwd = base / "backend_repo"
        other_cwd.mkdir()
        skills_block = "\n".join(f"          - {name}" for name in skill_names)
        policy_yaml = (
            "skill_source_roots:\n"
            f"  - {skills_root}\n"
            "rules:\n"
            "  - id: marketing-overlay\n"
            "    overlay: marketing\n"
            "    paths:\n"
            f"      - {marketing_cwd}\n"
            "    default: on\n"
            "    skills:\n"
            f"{skills_block}\n"
        )
        (config_root / "skill-scope.yaml").write_text(policy_yaml, encoding="utf-8")
        model = {"env": {"SKILLBOX_CLIENTS_HOST_ROOT": str(clients_root)}}
        return model, marketing_cwd, other_cwd

    def test_overlay_activate_is_policy_evaluated_not_literal_link(self) -> None:
        # Regression for repos-sbp-overlay-semantics-vq0.1: `overlay activate`
        # must run the SAME policy evaluation as `skill sync` with the named
        # overlay forced active for this call only — NOT blindly link every
        # literal overlay-tagged skill. In a cwd the overlay rule does not match,
        # the policy-correct set is empty (0), even though the overlay declares
        # many literal skills.
        skill_names = [f"mk{i:02d}" for i in range(35)]
        prior_env = os.environ.get(CLI.OVERLAY_ENV_VAR)
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            model, marketing_cwd, other_cwd = self._write_overlay_policy_fixture(base, skill_names)

            # The overlay literally declares all 35 skills (the old activate path
            # would have linked every one of these regardless of cwd).
            literal = CLI.overlay_scoped_skill_names(model, "marketing")
            self.assertEqual(len(literal), 35)

            # Non-matching cwd: policy evaluation links ZERO, not 35.
            non_matching = CLI.activate_overlay_scoped_skills(
                model, "marketing", str(other_cwd), to="project", dry_run=True
            )
            self.assertEqual(non_matching, [])
            self.assertFalse((other_cwd / ".claude" / "skills").exists())

            # Forcing the overlay active is ephemeral: no SKILLBOX_OVERLAYS state
            # persists past the call.
            self.assertEqual(os.environ.get(CLI.OVERLAY_ENV_VAR), prior_env)

            # Matching cwd: the policy-evaluated set is exactly the declared skills.
            def plan_link_destinations(activations: list[dict[str, object]]) -> dict[str, list[str]]:
                return {
                    str(activation["skill"]): sorted(
                        str(action.get("destination"))
                        for action in (activation.get("actions") or [])
                        if action.get("op") == "link"
                    )
                    for activation in activations
                }

            dry = CLI.activate_overlay_scoped_skills(
                model, "marketing", str(marketing_cwd), to="project", dry_run=True
            )
            self.assertEqual(sorted(a["skill"] for a in dry), sorted(skill_names))
            # --dry-run plan must equal the plan apply executes (zero-surprise links).
            applied = CLI.activate_overlay_scoped_skills(
                model, "marketing", str(marketing_cwd), to="project", dry_run=False
            )
            self.assertEqual(plan_link_destinations(dry), plan_link_destinations(applied))

            # apply actually created exactly the policy-evaluated symlinks, and
            # every activation carries a usable packet (SKILL.md + sha) so the
            # requesting agent can use the skill immediately.
            created = sorted(
                p.name for p in (marketing_cwd / ".claude" / "skills").iterdir()
            )
            self.assertEqual(created, sorted(skill_names))
            self.assertTrue(
                all(
                    a["activation_packet"] and a["activation_packet"]["skill_md_sha256"]
                    for a in applied
                )
            )

        # Env stays clean after the whole flow (no persisted overlay state).
        self.assertEqual(os.environ.get(CLI.OVERLAY_ENV_VAR), prior_env)

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
            mock.patch.object(CLI, "collect_mcp_audit", return_value={"summary": {"ok": True}}),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(
                CLI._handle_mcp_audit(  # noqa: SLF001
                    _ns(cwd="/repo", config_root="/repo", format="json"),
                    root,
                    model,
                    "reuse",
                ),
                CLI.EXIT_OK,
            )
        self.assertEqual(emitted[-1], {"summary": {"ok": True}})

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

    def test_handle_up_legacy_failed_start_returns_nonzero(self) -> None:
        emitted: list[dict[str, object]] = []
        root = Path("/tmp/skillbox")
        with (
            mock.patch.object(CLI, "local_runtime_active_profile", return_value=None),
            mock.patch.object(CLI, "select_services", return_value=[{"id": "api"}]),
            mock.patch.object(CLI, "sync_runtime", return_value=["sync"]),
            mock.patch.object(CLI, "resolve_services_for_start", return_value=[{"id": "api"}]),
            mock.patch.object(CLI, "resolve_tasks_for_services", return_value=[]),
            mock.patch.object(CLI, "select_env_files_for_tasks", return_value=[]),
            mock.patch.object(CLI, "select_env_files_for_services", return_value=[]),
            mock.patch.object(CLI, "ensure_required_env_files_ready"),
            mock.patch.object(CLI, "run_tasks", return_value=[]),
            mock.patch.object(CLI, "start_services", return_value=[{"id": "api", "result": "failed"}]),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            args = _ns(format="json", service=["api"], dry_run=False, wait_seconds=1.0)
            self.assertEqual(CLI._handle_up(args, root, {"bridges": []}, "reuse"), CLI.EXIT_ERROR)
        self.assertEqual(emitted[-1]["error"]["type"], "LOCAL_RUNTIME_START_BLOCKED")
        self.assertEqual(emitted[-1]["error"]["blocked_services"], ["api"])

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
        # Carrier change: even a bare RuntimeError raiser now gets the new
        # envelope keys (ok/error.code/error_code/deprecation) alongside legacy.
        self.assertIs(emitted[-1]["ok"], False)
        self.assertEqual(emitted[-1]["error"]["code"], "runtime_error")
        self.assertEqual(emitted[-1]["error_code"], "runtime_error")
        self.assertIn("deprecation", emitted[-1])

    def _run_dispatch_with_handler(self, handler, *, argv, emitted):
        def run_main(args: list[str]) -> int:
            with mock.patch.object(sys, "argv", ["manage.py", *args]):
                return CLI.main()

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch.dict(CLI._MODEL_DISPATCH, {"doctor": handler}),
            mock.patch.object(CLI, "build_runtime_model", return_value={}),
            mock.patch.object(CLI, "normalize_active_profiles", return_value=[]),
            mock.patch.object(CLI, "normalize_active_clients", return_value=[]),
            mock.patch.object(CLI, "filter_model", return_value={}),
            mock.patch.object(CLI, "_check_logs_deferred_surfaces", return_value=None),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            return run_main(["--root-dir", tmpdir, *argv])

    def test_typed_error_envelope_has_new_and_legacy_keys_coexisting(self) -> None:
        # ACCEPTANCE (1)+(2): a typed raise on a doctor/sync/up/down path emits
        # the envelope in JSON mode, and legacy keys (error, error_code,
        # error.type, error.recoverable) COEXIST with the deprecation marker.
        from runtime_manager.errors import ValidationError

        def typed_handler(args, root_dir, model, mode):
            raise ValidationError(
                "unknown_client",
                "Unknown runtime client(s): ghost.",
                context={"unknown": ["ghost"]},
            )

        emitted: list[dict[str, object]] = []
        exit_code = self._run_dispatch_with_handler(
            typed_handler, argv=["doctor", "--format", "json"], emitted=emitted
        )
        self.assertEqual(exit_code, CLI.EXIT_ERROR)
        payload = emitted[-1]
        # New canonical keys.
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error"]["code"], "unknown_client")
        self.assertEqual(payload["error"]["context"], {"unknown": ["ghost"]})
        # Legacy mirrors COEXIST.
        self.assertEqual(payload["error"]["type"], "unknown_client")
        self.assertTrue(payload["error"]["recoverable"])
        self.assertEqual(payload["error_code"], "unknown_client")
        # Deprecation marker present.
        self.assertEqual(payload["deprecation"]["use_instead"][0], "error.code")
        # The known message pattern still enriched recovery_hint/next_actions.
        self.assertIn("recovery_hint", payload["error"])
        self.assertIn("next_actions", payload)

    def test_unexpected_exception_emits_internal_envelope_without_traceback(self) -> None:
        # ACCEPTANCE (3): unknown exceptions -> generic INTERNAL envelope; the
        # traceback is NOT leaked unless --verbose is set.
        def boom_handler(args, root_dir, model, mode):
            raise ValueError("kaboom")

        emitted: list[dict[str, object]] = []
        exit_code = self._run_dispatch_with_handler(
            boom_handler, argv=["doctor", "--format", "json"], emitted=emitted
        )
        self.assertEqual(exit_code, CLI.EXIT_ERROR)
        payload = emitted[-1]
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error"]["code"], "INTERNAL")
        self.assertEqual(payload["error_code"], "INTERNAL")
        self.assertFalse(payload["error"]["recoverable"])
        self.assertIn("kaboom", payload["error"]["message"])
        # No traceback leaked by default.
        self.assertNotIn("context", payload["error"])

    def test_unexpected_exception_includes_traceback_when_verbose(self) -> None:
        def boom_handler(args, root_dir, model, mode):
            raise ValueError("kaboom")

        emitted: list[dict[str, object]] = []
        exit_code = self._run_dispatch_with_handler(
            boom_handler, argv=["--verbose", "doctor", "--format", "json"], emitted=emitted
        )
        self.assertEqual(exit_code, CLI.EXIT_ERROR)
        payload = emitted[-1]
        self.assertEqual(payload["error"]["code"], "INTERNAL")
        self.assertIn("traceback", payload["error"]["context"])
        self.assertIn("ValueError", payload["error"]["context"]["traceback"])

    def test_runtime_cwd_inference_prefers_client_with_local_runtime_service_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            shared_dep = root / "shared_service"
            shared_dep.mkdir()
            model = {
                "selection": {},
                "clients": [
                    {
                        "id": "personal",
                        "label": "Personal",
                        "default_cwd": str(root),
                        "context": {"cwd_match": [str(root)]},
                    },
                    {
                        "id": "cca",
                        "label": "CCA",
                        "default_cwd": str(root / "example-website"),
                        "context": {"cwd_match": [str(shared_dep)]},
                    },
                    {
                        "id": "app_core",
                        "label": "App Core",
                        "default_cwd": str(root / "app_core"),
                        "context": {"cwd_match": [str(shared_dep)]},
                    },
                ],
                "repos": [
                    {
                        "id": "shared_service",
                        "host_path": str(shared_dep),
                        "client": "app_core",
                    },
                    {
                        "id": "personal-ingredient",
                        "host_path": str(shared_dep),
                        "client": "personal",
                    },
                ],
                "services": [
                    {
                        "id": "personal-ingredient",
                        "client": "personal",
                        "repo": "personal-ingredient",
                        "profiles": ["local-all"],
                        "commands": {"reuse": "make personal-local-up"},
                    },
                    {
                        "id": "shared_service",
                        "client": "app_core",
                        "repo": "shared_service",
                        "profiles": ["local-all"],
                        "commands": {"reuse": "make local-up"},
                    },
                ],
                "artifacts": [],
                "env_files": [],
                "skills": [],
                "tasks": [],
                "logs": [],
                "checks": [],
                "bridges": [],
                "service_mode_commands": [],
                "ingress_routes": [],
                "parity_ledger": [],
            }

            args = _ns(command="up", cwd=str(shared_dep), profile=["local-all"])

            self.assertEqual(CLI._active_clients_for_args(args, model), {"app_core"})

    def test_skill_cwd_inference_keeps_visibility_match_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            shared_dep = root / "shared_service"
            shared_dep.mkdir()
            model = {
                "selection": {},
                "clients": [
                    {
                        "id": "personal",
                        "label": "Personal",
                        "default_cwd": str(root),
                        "context": {"cwd_match": [str(root)]},
                    },
                    {
                        "id": "cca",
                        "label": "CCA",
                        "default_cwd": str(root / "example-website"),
                        "context": {"cwd_match": [str(shared_dep)]},
                    },
                    {
                        "id": "service_app",
                        "label": "Service App",
                        "default_cwd": str(root / "service_app"),
                        "context": {"cwd_match": [str(shared_dep)]},
                    },
                ],
                "repos": [
                    {
                        "id": "shared_service",
                        "host_path": str(shared_dep),
                        "client": "service_app",
                    },
                    {
                        "id": "personal-ingredient",
                        "host_path": str(shared_dep),
                        "client": "personal",
                    },
                ],
                "services": [
                    {
                        "id": "personal-ingredient",
                        "client": "personal",
                        "repo": "personal-ingredient",
                        "profiles": ["local-all"],
                        "commands": {"reuse": "make personal-local-up"},
                    },
                    {
                        "id": "shared_service",
                        "client": "service_app",
                        "repo": "shared_service",
                        "profiles": ["local-all"],
                        "commands": {"reuse": "make local-up"},
                    },
                ],
                "artifacts": [],
                "env_files": [],
                "skills": [],
                "tasks": [],
                "logs": [],
                "checks": [],
                "bridges": [],
                "service_mode_commands": [],
                "ingress_routes": [],
                "parity_ledger": [],
            }

            args = _ns(command="skills", cwd=str(shared_dep), profile=["local-all"])

            self.assertEqual(CLI._active_clients_for_args(args, model), {"cca"})

    def test_runtime_cwd_inference_uses_existing_match_order_before_graph_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app_root = root / "example-app"
            app_root.mkdir()
            model = {
                "selection": {},
                "clients": [
                    {
                        "id": "example-app",
                        "label": "Sweet Potato",
                        "default_cwd": str(app_root),
                        "context": {"cwd_match": [str(app_root)]},
                    },
                    {
                        "id": "app_core",
                        "label": "App Core",
                        "default_cwd": str(root / "app_core"),
                        "context": {"cwd_match": [str(app_root)]},
                    },
                ],
                "repos": [
                    {
                        "id": "spaps-local",
                        "host_path": str(app_root),
                        "client": "example-app",
                    },
                    {
                        "id": "spaps-app_core",
                        "host_path": str(app_root),
                        "client": "app_core",
                    },
                    {
                        "id": "app_core-api",
                        "host_path": str(root / "api_server"),
                        "client": "app_core",
                    },
                ],
                "services": [
                    {
                        "id": "spaps-local",
                        "client": "example-app",
                        "repo": "spaps-local",
                        "profiles": ["local-all"],
                        "commands": {"reuse": "make local-up"},
                    },
                    {
                        "id": "spaps-app_core",
                        "client": "app_core",
                        "repo": "spaps-app_core",
                        "profiles": ["local-all"],
                        "commands": {"reuse": "make local-up"},
                    },
                    {
                        "id": "app_core-api",
                        "client": "app_core",
                        "repo": "app_core-api",
                        "profiles": ["local-all"],
                        "commands": {"reuse": "make local-up"},
                    },
                ],
                "artifacts": [],
                "env_files": [],
                "skills": [],
                "tasks": [],
                "logs": [],
                "checks": [],
                "bridges": [],
                "service_mode_commands": [],
                "ingress_routes": [],
                "parity_ledger": [],
            }

            args = _ns(command="up", cwd=str(app_root), profile=["local-all"])

            self.assertEqual(CLI._active_clients_for_args(args, model), {"example-app"})

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

        with (
            mock.patch.object(CLI, "skill_lifecycle_plan", return_value={"actions": []}),
            mock.patch.object(
                CLI,
                "apply_skill_lifecycle_plan",
                return_value={
                    "actions": [{"status": "skipped_pinned", "code": PRUNE_SKIPPED_PINNED}]
                },
            ),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_skill(skill_args, root, {}, "reuse"), CLI.EXIT_OK)

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

        parser = CLI._build_parser()
        parsed = parser.parse_args([
            "distribution-rollback",
            "--list",
            "--skill",
            "deploy",
            "--state-root",
            "state",
        ])
        self.assertTrue(parsed.list)
        self.assertIsNone(parsed.manifest_path)
        self.assertIsNone(parsed.public_key)
        self.assertIsNone(parsed.version)
        self.assertIsNone(parsed.lockfile)

        parsed = parser.parse_args([
            "mcp-audit",
            "--cwd",
            "/tmp/repo",
            "--config-root",
            "/tmp/config",
        ])
        self.assertEqual(parsed.command, "mcp-audit")
        self.assertEqual(parsed.cwd, "/tmp/repo")
        self.assertEqual(parsed.config_root, "/tmp/config")

        parsed = parser.parse_args([
            "skill",
            "prune",
            "--from",
            "project",
            "--cwd",
            "/tmp/repo",
        ])
        self.assertEqual(parsed.skill_action, "prune")
        self.assertEqual(parsed.from_scope, "project")
        self.assertEqual(parsed.cwd, "/tmp/repo")

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "bundle-cache" / "deploy"
            cache_dir.mkdir(parents=True)
            (cache_dir / "deploy-v2.skillbundle.tar.gz").write_bytes(b"two")
            (cache_dir / "deploy-v1.skillbundle.tar.gz").write_bytes(b"one")
            (cache_dir / "deploy-vbad.skillbundle.tar.gz").write_bytes(b"bad")
            self.assertEqual(
                CLI.cached_versions(state_root=Path(tmpdir), skill_name="deploy"),
                [1, 2],
            )

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

    def test_handle_restart_legacy_failed_start_returns_nonzero(self) -> None:
        emitted: list[dict[str, object]] = []
        root = Path("/tmp/skillbox")
        restart_args = _ns(service=["api"], dry_run=False, wait_seconds=1.0)
        with (
            mock.patch.object(CLI, "select_services", return_value=[{"id": "api"}]),
            mock.patch.object(CLI, "resolve_services_for_stop", return_value=[{"id": "api"}]),
            mock.patch.object(CLI, "resolve_services_for_start", return_value=[{"id": "api"}]),
            mock.patch.object(CLI, "resolve_tasks_for_services", return_value=[]),
            mock.patch.object(CLI, "stop_services", return_value=[{"id": "api", "result": "stopped"}]),
            mock.patch.object(CLI, "sync_runtime", return_value=["sync"]),
            mock.patch.object(CLI, "select_env_files_for_tasks", return_value=[]),
            mock.patch.object(CLI, "select_env_files_for_services", return_value=[]),
            mock.patch.object(CLI, "ensure_required_env_files_ready"),
            mock.patch.object(CLI, "run_tasks", return_value=[]),
            mock.patch.object(CLI, "start_services", return_value=[{"id": "api", "result": "timeout"}]),
            mock.patch.object(CLI, "emit_json", side_effect=emitted.append),
        ):
            self.assertEqual(CLI._handle_restart(restart_args, root, {}, "reuse"), CLI.EXIT_ERROR)

        self.assertEqual(emitted[-1]["error"]["type"], "LOCAL_RUNTIME_START_BLOCKED")
        self.assertEqual(emitted[-1]["error"]["blocked_services"], ["api"])


if __name__ == "__main__":
    unittest.main()
