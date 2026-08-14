"""PG-03/PG-04/PG-05: `box.py exec` and `box.py compose-down` gate parity.

`box.py exec` is the robot-CLI replacement for MCP ``operator_box_exec`` and
`box.py compose-down` the gated JSON replacement for the ungated text-mode
``make down``. Both are only safe to migrate onto if they behave EXACTLY like
the MCP surfaces they retire, so these tests pin:

- the classifier and command hash are the SAME objects for both surfaces
  (hoisted to lib.opslib), so a policy fix cannot land on one surface only
- the marker key binds box id + command hash, identically to the MCP key
- marker byte-interoperability in BOTH directions: a preview stamped by box.py
  is honoured by the operator MCP, and an MCP-stamped marker is honoured by
  box.py — an agent can migrate mid-flow without losing its preview
- read-only allowlisted commands take the fast path (no marker, no tree check)
- mutating/unknown commands are refused without a fresh matching marker, and a
  marker minted for command A never authorizes command B
- compose-down drives the same COMPOSEF stack the Makefile uses, previews with
  `compose ps`, and refuses the real down without a fresh marker
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
BOX_SCRIPT = ROOT_DIR / "scripts" / "box.py"
MCP_SCRIPT = ROOT_DIR / "scripts" / "operator_mcp_server.py"
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib import opslib  # noqa: E402

spec = importlib.util.spec_from_file_location("box_exec_test_module", BOX_SCRIPT)
assert spec and spec.loader
BOX = importlib.util.module_from_spec(spec)
sys.modules["box_exec_test_module"] = BOX
spec.loader.exec_module(BOX)

MCP = SourceFileLoader("box_exec_test_operator_mcp", str(MCP_SCRIPT.resolve())).load_module()


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["ssh"], returncode, stdout=stdout, stderr=stderr)


def _box(**overrides):
    fields = {
        "id": "gatebox",
        "profile": "dev-small",
        "state": "ready",
        "tailscale_ip": "100.100.0.9",
        "ssh_user": "skillbox",
    }
    fields.update(overrides)
    return BOX.Box(**fields)


def _dcg_allow_record(module) -> dict:
    """A deterministic 'guard allowed' record for tests that are NOT about DCG.

    `box.py exec` and operator_box_exec both fail closed when the pinned dcg
    binary is unavailable, so every gate/marker test must pin an explicit allow
    or it would silently be testing "no binary on this host" instead.
    """
    return {
        "verdict": "allow",
        "reason_code": "guard_allowed",
        "reason": "test stub",
        "available": True,
        "fail_closed": False,
        "decision": "allow",
        "warned": False,
        "binary": "/opt/pinned/dcg",
        "dcg_version": module.DCG_PINNED_VERSION,
        "expected_version": module.DCG_PINNED_VERSION,
        "interface": module.DCG_INTERFACE,
    }


def _patch_dcg_allow(module=BOX):
    return mock.patch.object(
        module, "evaluate_command_with_dcg", side_effect=lambda *a, **k: _dcg_allow_record(module)
    )


class _DcgAllowingTestCase(unittest.TestCase):
    """Base for gate tests whose subject is the marker policy, not the guard."""

    def setUp(self) -> None:
        super().setUp()
        self._dcg_patch = _patch_dcg_allow()
        self._dcg_patch.start()
        self.addCleanup(self._dcg_patch.stop)


class HoistedHelperParityTests(unittest.TestCase):
    """The policy helpers are ONE implementation, imported by both surfaces."""

    def test_classifier_and_hash_are_the_same_objects(self) -> None:
        for name in ("classify_box_exec_command", "command_hash"):
            with self.subTest(helper=name):
                self.assertIs(getattr(BOX, name), getattr(opslib, name))
                self.assertIs(getattr(MCP, name), getattr(opslib, name))
        # normalize_command feeds both of the above; the MCP also uses it for
        # audit redaction, so it must be the shared implementation too.
        self.assertIs(MCP.normalize_command, opslib.normalize_command)
        self.assertIs(BOX.box_exec_marker_key, opslib.box_exec_marker_key)

    def test_read_only_allowlist_and_mutating_verdicts(self) -> None:
        read_only = ["docker ps", "git status", "df -h", "uptime", "ls /srv"]
        mutating = [
            "docker exec workspace bash",
            "git push",
            "systemctl restart nginx",
            "cat /srv/.env",            # secret-looking path: preview required
            "ls /etc; whoami",          # shell chaining
            "FOO=bar ls",               # env prefix
            "/usr/bin/ls",              # path invocation
            "",                          # empty
        ]
        for command in read_only:
            with self.subTest(command=command):
                self.assertEqual(
                    BOX.classify_box_exec_command(command)["verdict"], "read-only"
                )
        for command in mutating:
            with self.subTest(command=command):
                self.assertEqual(
                    BOX.classify_box_exec_command(command)["verdict"], "mutating"
                )

    def test_command_hash_normalizes_whitespace_but_not_meaning(self) -> None:
        self.assertEqual(BOX.command_hash("docker   ps"), BOX.command_hash("docker ps"))
        self.assertNotEqual(BOX.command_hash("touch /a"), BOX.command_hash("touch /b"))


class MarkerKeyParityTests(unittest.TestCase):
    def test_box_and_mcp_derive_the_identical_marker_key(self) -> None:
        for box_id, command in (("gatebox", "systemctl restart nginx"), ("other", "touch /tmp/x")):
            with self.subTest(box_id=box_id):
                self.assertEqual(
                    BOX.box_exec_marker_key(box_id, command),
                    MCP._box_exec_marker_key(box_id, command),  # noqa: SLF001
                )

    def test_key_binds_both_box_and_command(self) -> None:
        base = BOX.box_exec_marker_key("gatebox", "touch /tmp/a")
        self.assertNotEqual(base, BOX.box_exec_marker_key("gatebox", "touch /tmp/b"))
        self.assertNotEqual(base, BOX.box_exec_marker_key("otherbox", "touch /tmp/a"))
        self.assertEqual(base, BOX.box_exec_marker_key("gatebox", "touch  /tmp/a"))


class MarkerInteropTests(unittest.TestCase):
    """Byte interop with the operator MCP marker store, in BOTH directions."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        root = Path(self.tmpdir.name)
        for module in (BOX, MCP):
            patch = mock.patch.object(module, "REPO_ROOT", root)
            patch.start()
            self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SKILLBOX_DRYRUN_MARKER_ROOT", None)
        self.key = BOX.box_exec_marker_key("gatebox", "systemctl restart nginx")

    def test_marker_paths_are_byte_identical(self) -> None:
        self.assertEqual(
            BOX._cli_dryrun_marker_path(BOX.BOX_EXEC_MARKER_TOOL, self.key),  # noqa: SLF001
            MCP._dryrun_marker_path(BOX.BOX_EXEC_MARKER_TOOL, self.key),  # noqa: SLF001
        )
        self.assertEqual(
            BOX._cli_dryrun_marker_path(  # noqa: SLF001
                BOX.COMPOSE_DOWN_MARKER_TOOL, BOX.COMPOSE_DOWN_MARKER_KEY
            ),
            MCP._dryrun_marker_path(  # noqa: SLF001
                BOX.COMPOSE_DOWN_MARKER_TOOL, BOX.COMPOSE_DOWN_MARKER_KEY
            ),
        )

    def test_cli_preview_authorizes_the_mcp_tool(self) -> None:
        BOX.stamp_cli_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, self.key)
        self.assertTrue(MCP._has_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, self.key))  # noqa: SLF001
        BOX.stamp_cli_dryrun_marker(BOX.COMPOSE_DOWN_MARKER_TOOL, BOX.COMPOSE_DOWN_MARKER_KEY)
        self.assertTrue(
            MCP._has_dryrun_marker(  # noqa: SLF001
                BOX.COMPOSE_DOWN_MARKER_TOOL, BOX.COMPOSE_DOWN_MARKER_KEY
            )
        )

    def test_mcp_preview_authorizes_the_cli(self) -> None:
        MCP._stamp_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, self.key)  # noqa: SLF001
        self.assertTrue(BOX.cli_dryrun_marker_valid(BOX.BOX_EXEC_MARKER_TOOL, self.key))
        MCP._stamp_dryrun_marker(  # noqa: SLF001
            BOX.COMPOSE_DOWN_MARKER_TOOL, BOX.COMPOSE_DOWN_MARKER_KEY
        )
        self.assertTrue(
            BOX.cli_dryrun_marker_valid(
                BOX.COMPOSE_DOWN_MARKER_TOOL, BOX.COMPOSE_DOWN_MARKER_KEY
            )
        )

    def test_marker_for_one_command_never_authorizes_another(self) -> None:
        BOX.stamp_cli_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, self.key)
        other = BOX.box_exec_marker_key("gatebox", "systemctl restart other")
        self.assertFalse(BOX.cli_dryrun_marker_valid(BOX.BOX_EXEC_MARKER_TOOL, other))
        self.assertFalse(MCP._has_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, other))  # noqa: SLF001


class ExecPlanTests(unittest.TestCase):
    def test_plan_quotes_argv_and_binds_the_marker(self) -> None:
        plan = BOX.box_exec_plan("gatebox", ["sh", "-c", "echo hi; echo bye"])
        self.assertEqual(plan["command"], "sh -c 'echo hi; echo bye'")
        self.assertEqual(plan["classification"]["verdict"], "mutating")
        self.assertEqual(
            plan["marker_key"], BOX.box_exec_marker_key("gatebox", plan["command"])
        )

    def test_remote_argv_after_separator_is_never_rewritten(self) -> None:
        """`--json` belongs to the REMOTE command, not to box.py."""
        normalized, _diagnostics = BOX._normalize_agent_argv(  # noqa: SLF001
            ["exec", "gatebox", "--json", "--", "docker", "logs", "--json", "svc"]
        )
        self.assertEqual(
            normalized,
            ["exec", "gatebox", "--format", "json", "--", "docker", "logs", "--json", "svc"],
        )


class ExecCommandTests(_DcgAllowingTestCase):
    def _run_exec(self, *, command_argv, dry_run=False, ssh_result=None, fmt="json"):
        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=[_box()]), \
             mock.patch.object(BOX, "ssh_cmd", return_value=ssh_result or _completed(0, "out\n")) as ssh, \
             mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            code = BOX.cmd_exec(
                "gatebox", command_argv=command_argv, dry_run=dry_run, fmt=fmt
            )
        return code, payloads, ssh

    def test_read_only_command_runs_and_reports_the_gate(self) -> None:
        code, payloads, ssh = self._run_exec(command_argv=["docker", "ps"])
        self.assertEqual(code, BOX.EXIT_OK)
        payload = payloads[-1]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["classification"], "read-only")
        self.assertEqual(payload["gate"], "read-only-allowlist")
        self.assertEqual(payload["command"], "docker ps")
        self.assertEqual(payload["command_hash"], BOX.command_hash("docker ps"))
        ssh.assert_called_once()
        self.assertEqual(ssh.call_args.args[2], "docker ps")

    def test_dry_run_previews_the_exact_command_without_running_it(self) -> None:
        code, payloads, ssh = self._run_exec(
            command_argv=["systemctl", "restart", "nginx"], dry_run=True
        )
        self.assertEqual(code, BOX.EXIT_OK)
        payload = payloads[-1]
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["classification"], "mutating")
        self.assertEqual(payload["would_run"]["command"], "systemctl restart nginx")
        self.assertEqual(
            payload["would_run"]["command_hash"], BOX.command_hash("systemctl restart nginx")
        )
        ssh.assert_not_called()

    def test_nonzero_remote_exit_is_a_structured_error(self) -> None:
        code, payloads, _ssh = self._run_exec(
            command_argv=["docker", "ps"], ssh_result=_completed(2, "", "boom\n")
        )
        self.assertEqual(code, BOX.EXIT_ERROR)
        self.assertFalse(payloads[-1]["ok"])
        self.assertEqual(payloads[-1]["error"]["type"], "remote_command_failed")
        self.assertEqual(payloads[-1]["exit_code"], 2)

    def test_missing_command_is_refused(self) -> None:
        code, payloads, ssh = self._run_exec(command_argv=[])
        self.assertEqual(code, BOX.EXIT_ERROR)
        self.assertEqual(payloads[-1]["error"]["type"], "missing_command")
        ssh.assert_not_called()

    def test_unknown_box_is_refused_before_ssh(self) -> None:
        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=[]), \
             mock.patch.object(BOX, "ssh_cmd") as ssh, \
             mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            code = BOX.cmd_exec("gatebox", command_argv=["docker", "ps"], fmt="json")
        self.assertEqual(code, BOX.EXIT_ERROR)
        self.assertEqual(payloads[-1]["error"]["type"], "box_not_found")
        ssh.assert_not_called()


class ExecDispatchGateTests(_DcgAllowingTestCase):
    """Drive main() so the gate wiring (not just the helpers) is pinned."""

    def _main(self, argv, *, dirty="", marker_valid=False, ssh_result=None):
        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=[_box()]), \
             mock.patch.object(BOX, "_repo_tree_dirty", return_value=dirty), \
             mock.patch.object(BOX, "cli_dryrun_marker_valid", return_value=marker_valid), \
             mock.patch.object(BOX, "stamp_cli_dryrun_marker") as stamp, \
             mock.patch.object(BOX, "clear_cli_dryrun_marker") as clear, \
             mock.patch.object(BOX, "ssh_cmd", return_value=ssh_result or _completed(0, "ok\n")) as ssh, \
             mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            code = BOX.main(argv)
        return code, payloads, stamp, clear, ssh

    def test_read_only_bypasses_the_gate_even_on_a_dirty_tree(self) -> None:
        code, payloads, _stamp, _clear, ssh = self._main(
            ["exec", "gatebox", "--format", "json", "--", "docker", "ps"],
            dirty="3 uncommitted path(s)",
        )
        self.assertEqual(code, BOX.EXIT_OK)
        self.assertEqual(payloads[-1]["gate"], "read-only-allowlist")
        ssh.assert_called_once()

    def test_mutating_without_marker_is_refused(self) -> None:
        code, payloads, _stamp, _clear, ssh = self._main(
            ["exec", "gatebox", "--format", "json", "--", "systemctl", "restart", "nginx"],
        )
        self.assertNotEqual(code, BOX.EXIT_OK)
        self.assertEqual(payloads[-1]["error"]["type"], "dryrun_marker_required")
        self.assertIn("--dry-run", " ".join(payloads[-1]["next_actions"]))
        ssh.assert_not_called()

    def test_mutating_on_a_dirty_tree_is_refused_before_the_marker_check(self) -> None:
        code, payloads, _stamp, _clear, ssh = self._main(
            ["exec", "gatebox", "--format", "json", "--", "systemctl", "restart", "nginx"],
            dirty="1 uncommitted path(s)",
            marker_valid=True,
        )
        self.assertNotEqual(code, BOX.EXIT_OK)
        self.assertEqual(payloads[-1]["error"]["type"], "dirty_tree_refused")
        ssh.assert_not_called()

    def test_dry_run_stamps_the_command_bound_marker(self) -> None:
        code, _payloads, stamp, _clear, ssh = self._main(
            ["exec", "gatebox", "--dry-run", "--format", "json", "--", "systemctl", "restart", "nginx"],
        )
        self.assertEqual(code, BOX.EXIT_OK)
        stamp.assert_called_once_with(
            BOX.BOX_EXEC_MARKER_TOOL,
            BOX.box_exec_marker_key("gatebox", "systemctl restart nginx"),
        )
        ssh.assert_not_called()

    def test_marker_authorizes_exactly_one_real_run(self) -> None:
        code, payloads, _stamp, clear, ssh = self._main(
            ["exec", "gatebox", "--format", "json", "--", "systemctl", "restart", "nginx"],
            marker_valid=True,
        )
        self.assertEqual(code, BOX.EXIT_OK)
        self.assertEqual(payloads[-1]["gate"], "dryrun-marker")
        ssh.assert_called_once()
        clear.assert_called_once_with(
            BOX.BOX_EXEC_MARKER_TOOL,
            BOX.box_exec_marker_key("gatebox", "systemctl restart nginx"),
        )


class ComposeStackTests(unittest.TestCase):
    def test_compose_argv_mirrors_the_makefile_composef(self) -> None:
        makefile = (ROOT_DIR / "Makefile").read_text(encoding="utf-8")
        self.assertIn(
            "COMPOSEF := $(COMPOSE) $(_ENV_FILE_ARG) -f docker-compose.yml -f $(_MONOSERVER_LAYER)",
            makefile,
            "Makefile COMPOSEF changed — box.py compose-down must follow it.",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(BOX, "REPO_ROOT", Path(tmpdir)), \
                 mock.patch.dict(os.environ, {"SKILLBOX_STATE_ROOT": str(Path(tmpdir) / ".skillbox-state")}):
                argv = BOX.compose_argv(["down"])
        self.assertEqual(
            argv,
            ["docker", "compose", "-f", "docker-compose.yml",
             "-f", "docker-compose.monoserver.yml", "down"],
        )

    def test_focused_client_override_layer_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "workspace" / ".compose-overrides").mkdir(parents=True)
            (root / "workspace" / ".focus.json").write_text(json.dumps({"client_id": "acme"}))
            (root / "workspace" / ".compose-overrides" / "docker-compose.client-acme.yml").write_text("{}")
            with mock.patch.object(BOX, "REPO_ROOT", root), \
                 mock.patch.dict(os.environ, {"SKILLBOX_STATE_ROOT": str(root / ".skillbox-state")}):
                argv = BOX.compose_argv(["down"])
        self.assertIn("workspace/.compose-overrides/docker-compose.client-acme.yml", argv)

    def test_operator_env_file_is_passed_like_the_makefile(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            operator_dir = root / ".skillbox-state" / "operator"
            operator_dir.mkdir(parents=True)
            (operator_dir / ".env").write_text("FOO=bar\n")
            with mock.patch.object(BOX, "REPO_ROOT", root), \
                 mock.patch.dict(os.environ, {"SKILLBOX_STATE_ROOT": str(root / ".skillbox-state")}):
                argv = BOX.compose_argv(["down"])
        self.assertEqual(argv[2], "--env-file")
        self.assertTrue(argv[3].endswith("/operator/.env"))


class ComposeDownGateTests(unittest.TestCase):
    def _main(self, argv, *, dirty="", marker_valid=False, compose=None):
        payloads: list[dict] = []
        compose = compose or (lambda args, timeout=300: (True, 0, {"exit_code": 0}))
        with mock.patch.object(BOX, "_repo_tree_dirty", return_value=dirty), \
             mock.patch.object(BOX, "cli_dryrun_marker_valid", return_value=marker_valid), \
             mock.patch.object(BOX, "stamp_cli_dryrun_marker") as stamp, \
             mock.patch.object(BOX, "clear_cli_dryrun_marker") as clear, \
             mock.patch.object(BOX, "run_compose", side_effect=compose) as run_compose, \
             mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            code = BOX.main(argv)
        return code, payloads, stamp, clear, run_compose

    def test_real_down_without_marker_is_refused(self) -> None:
        code, payloads, _stamp, _clear, run_compose = self._main(
            ["compose-down", "--format", "json"]
        )
        self.assertNotEqual(code, BOX.EXIT_OK)
        self.assertEqual(payloads[-1]["error"]["type"], "dryrun_marker_required")
        run_compose.assert_not_called()

    def test_real_down_on_a_dirty_tree_is_refused(self) -> None:
        code, payloads, _stamp, _clear, run_compose = self._main(
            ["compose-down", "--format", "json"], dirty="2 uncommitted path(s)", marker_valid=True
        )
        self.assertNotEqual(code, BOX.EXIT_OK)
        self.assertEqual(payloads[-1]["error"]["type"], "dirty_tree_refused")
        run_compose.assert_not_called()

    def test_dry_run_previews_with_compose_ps_and_stamps_the_marker(self) -> None:
        calls: list[list[str]] = []

        def compose(args, timeout=300):
            calls.append(args)
            return True, 0, [{"Service": "workspace", "State": "running"}]

        code, payloads, stamp, _clear, _rc = self._main(
            ["compose-down", "--dry-run", "--format", "json"], compose=compose
        )
        self.assertEqual(code, BOX.EXIT_OK)
        self.assertEqual(calls, [["ps", "--format", "json"]])
        self.assertTrue(payloads[-1]["dry_run"])
        self.assertEqual(payloads[-1]["would_stop"][0]["Service"], "workspace")
        self.assertIn("down", payloads[-1]["compose_command"])
        stamp.assert_called_once_with(
            BOX.COMPOSE_DOWN_MARKER_TOOL, BOX.COMPOSE_DOWN_MARKER_KEY
        )

    def test_marker_plus_clean_tree_runs_down_and_consumes_the_marker(self) -> None:
        calls: list[list[str]] = []

        def compose(args, timeout=300):
            calls.append(args)
            return True, 0, {"exit_code": 0}

        code, payloads, _stamp, clear, _rc = self._main(
            ["compose-down", "--format", "json"], marker_valid=True, compose=compose
        )
        self.assertEqual(code, BOX.EXIT_OK)
        self.assertEqual(calls, [["down"]])
        self.assertTrue(payloads[-1]["ok"])
        clear.assert_called_once_with(
            BOX.COMPOSE_DOWN_MARKER_TOOL, BOX.COMPOSE_DOWN_MARKER_KEY
        )

    def test_failed_preview_does_not_stamp_a_marker(self) -> None:
        def compose(args, timeout=300):
            return False, 1, {"exit_code": 1, "stderr": "docker daemon not running"}

        code, payloads, stamp, _clear, _rc = self._main(
            ["compose-down", "--dry-run", "--format", "json"], compose=compose
        )
        self.assertEqual(code, BOX.EXIT_ERROR)
        self.assertEqual(payloads[-1]["error"]["type"], "compose_preview_failed")
        stamp.assert_not_called()


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# PG-06: DCG gate parity (skillbox-u0o3)
#
# The marker gate proves the operator PREVIEWED a command. The destructive
# command guard is the other half of the policy, and until this bead the CLI
# did not run it at all: `box.py exec` would execute a command that MCP
# operator_box_exec would have refused. These tests pin the two surfaces to ONE
# adapter (lib.dcglib) and to identical decisions for identical guard answers.
#
# RISK GATE: no test in this section executes the command it inspects. The
# destructive fixture is textually destructive but referentially inert (its
# `rm -rf` targets a nonexistent path; its only creative clause touches a
# sentinel inside a throwaway temp dir), ssh is mocked and asserted un-called on
# every deny path, and tearDown asserts the sentinel never appeared.
# ---------------------------------------------------------------------------

from lib import dcglib  # noqa: E402


def _mcp_payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _robot(module, **fields) -> str:
    report = {
        "schema_version": module.DCG_ROBOT_SCHEMA_VERSION,
        "dcg_version": module.DCG_PINNED_VERSION.lstrip("v"),
        "robot_mode": True,
    }
    report.update(fields)
    return json.dumps(report)


class DcgAdapterHoistParityTests(unittest.TestCase):
    """One adapter implementation, re-imported into each surface's namespace."""

    def test_both_surfaces_delegate_to_the_same_dcglib(self) -> None:
        self.assertIs(BOX._dcglib, dcglib)  # noqa: SLF001
        self.assertIs(MCP._dcglib, dcglib)  # noqa: SLF001

    def test_blocks_execution_predicate_is_the_shared_one(self) -> None:
        for module in (BOX, MCP):
            with self.subTest(surface=module.__name__):
                for verdict in (None, {}, {"verdict": "deny"}, {"verdict": "unavailable"}):
                    self.assertTrue(module.dcg_blocks_execution(verdict))
                self.assertFalse(module.dcg_blocks_execution({"verdict": "allow"}))

    def test_version_pin_and_interface_are_identical(self) -> None:
        self.assertEqual(BOX.DCG_PINNED_VERSION, MCP.DCG_PINNED_VERSION)
        self.assertEqual(BOX.DCG_PIN_IMPORT_ERROR, MCP.DCG_PIN_IMPORT_ERROR)
        self.assertEqual(BOX.DCG_INTERFACE, MCP.DCG_INTERFACE)
        self.assertEqual(BOX.DCG_ROBOT_SCHEMA_VERSION, MCP.DCG_ROBOT_SCHEMA_VERSION)
        # The pin is consumed, never re-declared.
        self.assertEqual(BOX.DCG_PINNED_VERSION, "v0.6.7")
        self.assertEqual(BOX.DCG_PIN_IMPORT_ERROR, "")

    def test_neither_surface_re_declares_the_adapter_body(self) -> None:
        """The adapter logic exists once. A copy would drift silently."""
        for script in (BOX_SCRIPT, MCP_SCRIPT):
            source = script.read_text(encoding="utf-8")
            with self.subTest(script=script.name):
                self.assertNotIn('"test", "--robot"', source)
                self.assertNotIn("shutil.which(DCG_BINARY_NAME)", source)
                self.assertNotIn('frozenset({"allow", "warn"})', source)
                self.assertNotIn('"fail_closed": verdict ==', source)

    def test_refusal_text_and_remediation_come_from_one_helper(self) -> None:
        for reason_key, verdict in (
            ("unavailable", {"fail_closed": True, "reason": "no binary"}),
            ("denied", {"fail_closed": False, "reason": "boom", "rule_id": "core:rm"}),
        ):
            box_denial = dcglib.dcg_denial(BOX.DCG_SURFACE_NAME, dict(verdict))
            mcp_denial = dcglib.dcg_denial(MCP.DCG_SURFACE_NAME, dict(verdict))
            with self.subTest(case=reason_key):
                self.assertEqual(box_denial["error_type"], mcp_denial["error_type"])
                self.assertEqual(box_denial["recoverable"], mcp_denial["recoverable"])
                # Same remediation; only the surface name differs in the prose.
                self.assertEqual(
                    [a.replace(MCP.DCG_SURFACE_NAME, BOX.DCG_SURFACE_NAME) for a in mcp_denial["next_actions"]],
                    box_denial["next_actions"],
                )
                self.assertEqual(
                    mcp_denial["message"].replace(MCP.DCG_SURFACE_NAME, BOX.DCG_SURFACE_NAME),
                    box_denial["message"],
                )


class DcgVerdictParityTests(unittest.TestCase):
    """Identical guard answers must produce identical verdicts on both surfaces."""

    # (label, run_checked result, expected verdict, expected reason_code)
    CASES = (
        ("allow", {"rc": 0, "decision": "allow"}, "allow", "guard_allowed"),
        ("warn", {"rc": 0, "decision": "warn"}, "allow", "guard_allowed"),
        ("deny", {"rc": 1, "decision": "deny"}, "deny", "guard_denied"),
        ("block", {"rc": 1, "decision": "block"}, "deny", "guard_denied"),
        ("unknown_decision", {"rc": 0, "decision": "maybe"}, "unavailable", "unsupported_response"),
        ("missing_decision", {"rc": 0}, "unavailable", "unsupported_response"),
    )

    def _evaluate(self, module, command, *, rc, stdout):
        result = {"rc": rc, "stdout": stdout, "stderr_redacted": "", "elapsed": 0.01}
        with mock.patch.object(module, "_dcg_binary_path", return_value="/opt/pinned/dcg"), \
             mock.patch.object(module, "run_checked", return_value=result):
            return module.evaluate_command_with_dcg(command)

    def test_same_robot_response_same_verdict_on_both_surfaces(self) -> None:
        for label, fields, expected_verdict, expected_reason in self.CASES:
            rc = fields.pop("rc")
            with self.subTest(case=label):
                box_verdict = self._evaluate(BOX, "systemctl restart nginx", rc=rc, stdout=_robot(BOX, **fields))
                mcp_verdict = self._evaluate(MCP, "systemctl restart nginx", rc=rc, stdout=_robot(MCP, **fields))
                self.assertEqual(box_verdict, mcp_verdict)
                self.assertEqual(box_verdict["verdict"], expected_verdict)
                self.assertEqual(box_verdict["reason_code"], expected_reason)
                self.assertEqual(
                    BOX.dcg_blocks_execution(box_verdict), MCP.dcg_blocks_execution(mcp_verdict)
                )

    def test_every_failure_mode_fails_closed_on_both_surfaces(self) -> None:
        failures = (
            ("malformed_output", {"rc": 0, "stdout": "not json"}),
            ("empty_output", {"rc": 0, "stdout": ""}),
            ("json_array", {"rc": 0, "stdout": "[]"}),
            ("timeout", {"rc": -1, "stdout": "", "error_code": "TIMEOUT"}),
            ("spawn_failure", {"rc": -1, "stdout": "", "error_code": "ENOENT"}),
        )
        for label, result_fields in failures:
            with self.subTest(case=label):
                result = {"stderr_redacted": "", "elapsed": 0.0}
                result.update(result_fields)
                verdicts = []
                for module in (BOX, MCP):
                    with mock.patch.object(module, "_dcg_binary_path", return_value="/opt/pinned/dcg"), \
                         mock.patch.object(module, "run_checked", return_value=dict(result)):
                        verdicts.append(module.evaluate_command_with_dcg("systemctl restart nginx"))
                self.assertEqual(verdicts[0], verdicts[1])
                self.assertEqual(verdicts[0]["verdict"], "unavailable")
                self.assertTrue(verdicts[0]["fail_closed"])
                self.assertTrue(BOX.dcg_blocks_execution(verdicts[0]))

    def test_incompatible_schema_and_version_fail_closed_on_both(self) -> None:
        for label, stdout_fields in (
            ("wrong_schema", {"schema_version": 99}),
            ("wrong_version", {"dcg_version": "0.5.1", "decision": "allow"}),
            ("unparseable_version", {"dcg_version": "", "decision": "allow"}),
        ):
            with self.subTest(case=label):
                verdicts = []
                for module in (BOX, MCP):
                    report = json.loads(_robot(module, decision="allow"))
                    report.update(stdout_fields)
                    result = {"rc": 0, "stdout": json.dumps(report), "stderr_redacted": "", "elapsed": 0.0}
                    with mock.patch.object(module, "_dcg_binary_path", return_value="/opt/pinned/dcg"), \
                         mock.patch.object(module, "run_checked", return_value=result):
                        verdicts.append(module.evaluate_command_with_dcg("docker ps"))
                self.assertEqual(verdicts[0], verdicts[1])
                self.assertEqual(verdicts[0]["reason_code"], "incompatible_version")
                self.assertTrue(verdicts[0]["fail_closed"])

    def test_missing_binary_fails_closed_on_both_surfaces(self) -> None:
        verdicts = []
        for module in (BOX, MCP):
            with mock.patch.object(module, "_dcg_binary_path", return_value=""):
                verdicts.append(module.evaluate_command_with_dcg("docker ps"))
        self.assertEqual(verdicts[0], verdicts[1])
        self.assertEqual(verdicts[0]["reason_code"], "binary_missing")
        self.assertTrue(verdicts[0]["fail_closed"])

    def test_command_reaches_dcg_as_one_argv_element_on_both_surfaces(self) -> None:
        payload = "rm -rf /nonexistent-skillbox-fixture ; touch /tmp/never"
        for module in (BOX, MCP):
            captured: dict = {}

            def fake_run_checked(cmd, **kwargs):
                captured["cmd"] = list(cmd)
                captured["kwargs"] = kwargs
                return {"rc": 1, "stdout": _robot(module, decision="deny"), "stderr_redacted": ""}

            with mock.patch.object(module, "_dcg_binary_path", return_value="/opt/pinned/dcg"), \
                 mock.patch.object(module, "run_checked", side_effect=fake_run_checked):
                module.evaluate_command_with_dcg(payload)

            with self.subTest(surface=module.__name__):
                self.assertEqual(captured["cmd"][:2], ["/opt/pinned/dcg", "test"])
                self.assertEqual(captured["cmd"][-2], "--")
                self.assertEqual(captured["cmd"][-1], payload)
                self.assertEqual(captured["cmd"].count(payload), 1)
                self.assertNotIn("shell", captured["kwargs"])
                self.assertIsNone(captured["kwargs"].get("input_text"))


class DcgExecutionPathParityTests(unittest.TestCase):
    """End to end: the same guard answer opens or closes BOTH real-run paths."""

    MCP_READY_BOX = {
        "id": "gatebox",
        "state": "ready",
        "tailscale_ip": "100.100.0.9",
        "ssh_user": "skillbox",
    }

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        for module in (BOX, MCP):
            patch = mock.patch.object(module, "REPO_ROOT", root)
            patch.start()
            self.addCleanup(patch.stop)
        # Execution sentinel: created ONLY if the payload is ever actually run.
        self.sentinel = root / "dcg-payload-executed.sentinel"
        self.destructive_fixture = (
            "rm -rf /nonexistent-skillbox-dcg-fixture-does-not-exist "
            f"; touch {self.sentinel}"
        )

    def tearDown(self) -> None:
        sentinel_present = self.sentinel.exists()
        self._tmp.cleanup()
        self.assertFalse(
            sentinel_present,
            "RISK GATE VIOLATION: the destructive fixture was executed.",
        )

    # -- helpers ---------------------------------------------------------

    def _run_box(self, command_argv, *, rc, stdout, dry_run=False):
        payloads: list[dict] = []
        result = {"rc": rc, "stdout": stdout, "stderr_redacted": "", "elapsed": 0.0}
        with mock.patch.object(BOX, "load_inventory", return_value=[_box()]), \
             mock.patch.object(BOX, "_dcg_binary_path", return_value="/opt/pinned/dcg"), \
             mock.patch.object(BOX, "run_checked", return_value=result), \
             mock.patch.object(BOX, "ssh_cmd", return_value=_completed(0, "ok\n")) as ssh, \
             mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            code = BOX.cmd_exec(
                "gatebox", command_argv=command_argv, dry_run=dry_run, fmt="json"
            )
        return code, payloads[-1], ssh

    def _run_mcp(self, command, *, rc, stdout, dry_run=False):
        result = {"rc": rc, "stdout": stdout, "stderr_redacted": "", "elapsed": 0.0}
        params = {"box_id": "gatebox", "command": command}
        if dry_run:
            params["dry_run"] = True
        with mock.patch.object(MCP, "find_box", return_value=self.MCP_READY_BOX), \
             mock.patch.object(MCP, "_dcg_binary_path", return_value="/opt/pinned/dcg"), \
             mock.patch.object(MCP, "run_checked", return_value=result), \
             mock.patch.object(MCP, "run_ssh", return_value=(True, 0, {"stdout": "ok"})) as run_ssh:
            envelope = MCP.handle_operator_box_exec(params)
        return envelope, _mcp_payload(envelope), run_ssh

    # -- the read-only fast path is NOT a guard bypass --------------------

    def test_readonly_fast_path_is_guarded_on_both_surfaces(self) -> None:
        """`docker ps` skips the MARKER, never the GUARD."""
        deny = _robot(BOX, decision="deny", rule_id="core.test:deny-all", reason="nope")
        code, payload, ssh = self._run_box(["docker", "ps"], rc=1, stdout=deny)
        self.assertEqual(code, BOX.EXIT_ERROR)
        self.assertEqual(payload["error"]["type"], "dcg_denied")
        self.assertFalse(payload["executed"])
        ssh.assert_not_called()

        envelope, mcp_payload, run_ssh = self._run_mcp("docker ps", rc=1, stdout=deny)
        self.assertTrue(envelope["isError"])
        self.assertEqual(mcp_payload["error"]["type"], "dcg_denied")
        self.assertFalse(mcp_payload["error"]["executed"])
        run_ssh.assert_not_called()

    def test_unavailable_guard_blocks_the_readonly_path_on_both_surfaces(self) -> None:
        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=[_box()]), \
             mock.patch.object(BOX, "_dcg_binary_path", return_value=""), \
             mock.patch.object(BOX, "ssh_cmd") as ssh, \
             mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            code = BOX.cmd_exec("gatebox", command_argv=["docker", "ps"], fmt="json")
        self.assertEqual(code, BOX.EXIT_ERROR)
        self.assertEqual(payloads[-1]["error"]["type"], "dcg_unavailable")
        self.assertEqual(payloads[-1]["dcg"]["reason_code"], "binary_missing")
        ssh.assert_not_called()

        with mock.patch.object(MCP, "find_box", return_value=self.MCP_READY_BOX), \
             mock.patch.object(MCP, "_dcg_binary_path", return_value=""), \
             mock.patch.object(MCP, "run_ssh") as run_ssh:
            envelope = MCP.handle_operator_box_exec({"box_id": "gatebox", "command": "docker ps"})
        payload = _mcp_payload(envelope)
        self.assertEqual(payload["error"]["type"], "dcg_unavailable")
        self.assertEqual(payload["error"]["dcg"]["reason_code"], "binary_missing")
        run_ssh.assert_not_called()

    def test_allow_lets_the_readonly_path_through_on_both_surfaces(self) -> None:
        allow = _robot(BOX, decision="allow")
        code, payload, ssh = self._run_box(["docker", "ps"], rc=0, stdout=allow)
        self.assertEqual(code, BOX.EXIT_OK)
        self.assertEqual(payload["gate"], "read-only-allowlist")
        ssh.assert_called_once()

        envelope, mcp_payload, run_ssh = self._run_mcp("docker ps", rc=0, stdout=allow)
        self.assertFalse(envelope.get("isError", False))
        self.assertEqual(mcp_payload["stdout"], "ok")
        run_ssh.assert_called_once()

    # -- the marker path is NOT a guard bypass either ---------------------

    def test_a_valid_marker_does_not_override_a_guard_deny(self) -> None:
        """The marker proves a preview happened, not that the command is safe."""
        deny = _robot(BOX, decision="deny", rule_id="core.filesystem:rm-rf-general", reason="destructive")
        result = {"rc": 1, "stdout": deny, "stderr_redacted": "", "elapsed": 0.0}
        argv = ["sh", "-c", self.destructive_fixture]
        command = BOX.box_exec_command_string(argv)
        marker_key = BOX.box_exec_marker_key("gatebox", command)
        # Both surfaces derive the SAME key, so one stamp authorizes both.
        self.assertEqual(marker_key, MCP._box_exec_marker_key("gatebox", command))  # noqa: SLF001
        BOX.stamp_cli_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, marker_key)
        self.assertTrue(MCP._has_dryrun_marker("operator_box_exec", marker_key))  # noqa: SLF001

        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=[_box()]), \
             mock.patch.object(BOX, "_repo_tree_dirty", return_value=""), \
             mock.patch.object(BOX, "_dcg_binary_path", return_value="/opt/pinned/dcg"), \
             mock.patch.object(BOX, "run_checked", return_value=dict(result)), \
             mock.patch.object(BOX, "ssh_cmd") as ssh, \
             mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            code = BOX.main(["exec", "gatebox", "--format", "json", "--"] + argv)
        self.assertEqual(code, BOX.EXIT_ERROR)
        self.assertEqual(payloads[-1]["error"]["type"], "dcg_denied")
        self.assertFalse(payloads[-1]["executed"])
        ssh.assert_not_called()

        with mock.patch.object(MCP, "find_box", return_value=self.MCP_READY_BOX), \
             mock.patch.object(MCP, "_dcg_binary_path", return_value="/opt/pinned/dcg"), \
             mock.patch.object(MCP, "run_checked", return_value=dict(result)), \
             mock.patch.object(MCP, "run_ssh") as run_ssh:
            envelope = MCP.handle_operator_box_exec({"box_id": "gatebox", "command": command})
        self.assertEqual(_mcp_payload(envelope)["error"]["type"], "dcg_denied")
        run_ssh.assert_not_called()

    # -- the preview is advisory on both surfaces -------------------------

    def test_dry_run_preview_is_advisory_and_flags_the_real_run_on_both(self) -> None:
        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=[_box()]), \
             mock.patch.object(BOX, "_dcg_binary_path", return_value=""), \
             mock.patch.object(BOX, "ssh_cmd") as ssh, \
             mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            code = BOX.cmd_exec(
                "gatebox",
                command_argv=["systemctl", "restart", "nginx"],
                dry_run=True,
                fmt="json",
            )
        self.assertEqual(code, BOX.EXIT_OK)
        box_dcg = payloads[-1]["dcg"]

        with mock.patch.object(MCP, "find_box", return_value=self.MCP_READY_BOX), \
             mock.patch.object(MCP, "_dcg_binary_path", return_value=""), \
             mock.patch.object(MCP, "run_ssh") as run_ssh:
            envelope = MCP.handle_operator_box_exec(
                {"box_id": "gatebox", "command": "systemctl restart nginx", "dry_run": True}
            )
        mcp_dcg = _mcp_payload(envelope)["dcg"]

        for label, dcg in (("box", box_dcg), ("mcp", mcp_dcg)):
            with self.subTest(surface=label):
                self.assertFalse(dcg["authoritative"])
                self.assertFalse(dcg["blocks_execution_here"])
                self.assertTrue(dcg["blocks_real_run"])
                self.assertEqual(dcg["reason_code"], "binary_missing")
        ssh.assert_not_called()
        run_ssh.assert_not_called()

    def test_advisory_site_names_are_declared_and_enforced(self) -> None:
        self.assertEqual(BOX.BOX_EXEC_DCG_ADVISORY_SITES, ("box_exec:dry_run_preview",))
        self.assertEqual(MCP.DCG_ADVISORY_SITES, ("operator_box_exec:dry_run_preview",))
        with self.assertRaises(ValueError):
            BOX.dcg_advisory("ls", site="box_exec:real_run")
        with self.assertRaises(ValueError):
            MCP.dcg_advisory("ls", site="operator_box_exec:real_run")


# ---------------------------------------------------------------------------
# PG-07: `box.py compose-up` — the last tool-parity gap (skillbox-e11d)
#
# operator_compose_up was the only MCP tool with no robot-JSON CLI equivalent,
# which is what kept the operator MCP from retiring. These tests pin the new
# verb to the MCP handler's actual behaviour (step order, optional surfaces,
# build-failure short circuit) and — importantly — pin the DELIBERATE ABSENCE of
# a marker gate, so nobody "fixes" it into existence without reading why.
#
# RISK GATE: run_compose is mocked in every test. No docker command is executed.
# ---------------------------------------------------------------------------

class ComposeUpTests(unittest.TestCase):
    def _main(self, argv, *, dirty="", marker_valid=False, compose=None):
        payloads: list[dict] = []
        calls: list[list[str]] = []

        def default(args, timeout=300):
            calls.append(list(args))
            return True, 0, {"exit_code": 0}

        inner = compose or default

        def recording(args, timeout=300):
            if compose is None:
                return inner(args, timeout=timeout)
            calls.append(list(args))
            return inner(args, timeout=timeout)

        with mock.patch.object(BOX, "_repo_tree_dirty", return_value=dirty), \
             mock.patch.object(BOX, "cli_dryrun_marker_valid", return_value=marker_valid), \
             mock.patch.object(BOX, "stamp_cli_dryrun_marker") as stamp, \
             mock.patch.object(BOX, "run_compose", side_effect=recording), \
             mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            code = BOX.main(argv)
        return code, payloads, calls, stamp

    # --- the gating decision, pinned ---------------------------------------

    def test_real_up_needs_no_marker_and_no_clean_tree(self) -> None:
        """DELIBERATE: constructive verbs are not marker-gated. See gate_policy.

        A dirty tree is the NORMAL state when you start a dev stack. Gating this
        would train operators to set SKILLBOX_CLI_MUTATION_GATE=skip, which is
        strictly worse than not gating.
        """
        code, payloads, calls, _stamp = self._main(
            ["compose-up", "--format", "json"],
            dirty="7 uncommitted path(s)",
            marker_valid=False,
        )
        self.assertEqual(code, BOX.EXIT_OK)
        self.assertTrue(payloads[-1]["ok"])
        self.assertEqual(calls, [["build"], ["up", "-d"]])

    def test_dry_run_starts_nothing_and_stamps_no_marker(self) -> None:
        code, payloads, calls, stamp = self._main(
            ["compose-up", "--dry-run", "--format", "json"]
        )
        self.assertEqual(code, BOX.EXIT_OK)
        # Only the read-only probe ran; no build, no up.
        self.assertEqual(calls, [["ps", "--format", "json"]])
        stamp.assert_not_called()
        payload = payloads[-1]
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["gated"])
        self.assertEqual(
            [step["step"] for step in payload["steps"]], ["build", "up"]
        )

    def test_gate_policy_is_published_with_its_reasoning(self) -> None:
        command = BOX._box_agent_command("compose-up")
        policy = command["gate_policy"]
        self.assertFalse(policy["marker_required"])
        self.assertFalse(policy["clean_tree_required"])
        self.assertIn("compose-down", policy["rationale"])
        self.assertTrue(command["mutates"])
        self.assertFalse(command["destructive"])

    def test_the_destructive_inverse_is_still_gated(self) -> None:
        """The reason compose-up may go ungated is that compose-down does not."""
        self.assertTrue(BOX._box_agent_command("compose-down")["destructive"])
        code, payloads, _calls, _stamp = self._main(["compose-down", "--format", "json"])
        self.assertNotEqual(code, BOX.EXIT_OK)
        self.assertEqual(payloads[-1]["error"]["type"], "dryrun_marker_required")

    # --- parity with the MCP handler ---------------------------------------

    def test_preview_and_real_run_walk_the_same_step_plan(self) -> None:
        """One source of truth, so the preview cannot lie about the real run."""
        for build, surfaces in ((True, False), (False, False), (True, True), (False, True)):
            with self.subTest(build=build, surfaces=surfaces):
                planned = [
                    step["args"]
                    for step in BOX.compose_up_steps(build=build, surfaces=surfaces)
                ]
                argv = ["compose-up", "--format", "json"]
                if not build:
                    argv.append("--no-build")
                if surfaces:
                    argv.append("--surfaces")
                _code, _payloads, calls, _stamp = self._main(argv)
                self.assertEqual(calls, planned)

    def test_step_plan_matches_the_mcp_handler_invocations(self) -> None:
        """Same compose subcommands, same order, as handle_operator_compose_up."""
        self.assertEqual(
            [step["args"] for step in BOX.compose_up_steps(build=True, surfaces=True)],
            [["build"], ["up", "-d"], ["--profile", "surfaces", "up", "-d"]],
        )

    def test_optional_surface_failure_is_reported_but_not_fatal(self) -> None:
        def compose(args, timeout=300):
            if args[:1] == ["--profile"]:
                return False, 1, {"exit_code": 1}
            return True, 0, {"exit_code": 0}

        code, payloads, _calls, _stamp = self._main(
            ["compose-up", "--surfaces", "--format", "json"], compose=compose
        )
        self.assertEqual(code, BOX.EXIT_OK)
        payload = payloads[-1]
        self.assertTrue(payload["headline_ok"])
        # ok tracks the headline, so it never contradicts the exit code.
        self.assertTrue(payload["ok"])
        self.assertEqual([s["step"] for s in payload["partial_failures"]], ["up-surfaces"])

    def test_build_failure_short_circuits_before_up(self) -> None:
        def compose(args, timeout=300):
            return (False, 1, {"exit_code": 1}) if args == ["build"] else (True, 0, {})

        code, payloads, calls, _stamp = self._main(
            ["compose-up", "--format", "json"], compose=compose
        )
        self.assertEqual(code, BOX.EXIT_ERROR)
        self.assertEqual(calls, [["build"]])
        self.assertEqual(payloads[-1]["error"]["type"], "build_failed")

    def test_headline_up_failure_is_an_error_exit(self) -> None:
        def compose(args, timeout=300):
            return (False, 1, {"exit_code": 1}) if args == ["up", "-d"] else (True, 0, {})

        code, payloads, _calls, _stamp = self._main(
            ["compose-up", "--no-build", "--surfaces", "--format", "json"], compose=compose
        )
        self.assertEqual(code, BOX.EXIT_ERROR)
        self.assertFalse(payloads[-1]["headline_ok"])
        self.assertEqual(payloads[-1]["error"]["type"], "up_failed")

    def test_it_drives_the_same_composef_stack_as_the_makefile(self) -> None:
        argv = BOX.compose_argv(["up", "-d"])
        self.assertEqual(argv[:2], ["docker", "compose"])
        self.assertIn("docker-compose.yml", argv)
        self.assertEqual(argv[-2:], ["up", "-d"])

    def test_preview_survives_a_docker_probe_failure(self) -> None:
        """The plan is knowable without docker; a dead daemon must not hide it."""
        def compose(args, timeout=300):
            return False, 1, {"exit_code": 1, "stderr": "daemon not running"}

        code, payloads, _calls, _stamp = self._main(
            ["compose-up", "--dry-run", "--format", "json"], compose=compose
        )
        self.assertEqual(code, BOX.EXIT_OK)
        self.assertIn("probe_failed", payloads[-1]["current_state"])
        self.assertEqual([s["step"] for s in payloads[-1]["steps"]], ["build", "up"])

    def test_verb_is_registered_as_a_json_surface(self) -> None:
        self.assertIn("compose-up", BOX.BOX_COMMAND_NAMES)
        self.assertIn("compose-up", BOX.BOX_JSON_COMMANDS)
        equivalents = BOX.box_capabilities_payload()["mcp_equivalents"]
        self.assertEqual(equivalents["compose-up"], "operator_compose_up")


# ---------------------------------------------------------------------------
# PG-07: marker interop is SYMMETRIC, and one preview buys one ATTEMPT
#
# Two defects found by integration review, pinned here so they cannot return:
#
# 1. The two surfaces hashed differently-quoted spellings of the SAME command.
#    box.py exec shlex-joins the argv after `--`; the MCP takes the string the
#    caller typed. `sh -c "echo hi"` and `sh -c 'echo hi'` therefore landed on
#    different marker keys, so a preview on one surface could not authorize the
#    run on the other — the whole point of sharing the store. Both now hash
#    opslib.canonical_command(...).
#
# 2. Markers were consumed only on EXIT_OK, so a mutating command that failed
#    AFTER mutating left a replayable marker for the rest of the TTL. Both
#    surfaces now consume ON DISPATCH.
#
# RISK GATE: every ssh/compose/dcg call is mocked; nothing here runs a command.
# ---------------------------------------------------------------------------

class CommandCanonicalizationTests(unittest.TestCase):
    """One canonicalizer, in opslib, feeding the hash on BOTH surfaces."""

    QUOTED_ARGV = ["sh", "-c", "echo hi"]

    def test_quoting_style_no_longer_splits_the_marker_key(self) -> None:
        cli_command = BOX.box_exec_command_string(self.QUOTED_ARGV)
        typed_command = 'sh -c "echo hi"'
        # Precondition: this is exactly the pair that used to diverge, because
        # the raw/whitespace-normalized spellings differ.
        self.assertNotEqual(
            opslib.normalize_command(cli_command),
            opslib.normalize_command(typed_command),
        )
        self.assertEqual(
            opslib.canonical_command(cli_command),
            opslib.canonical_command(typed_command),
        )
        self.assertEqual(BOX.command_hash(cli_command), MCP.command_hash(typed_command))
        self.assertEqual(
            BOX.box_exec_marker_key("gatebox", cli_command),
            MCP._box_exec_marker_key("gatebox", typed_command),  # noqa: SLF001
        )

    def test_canonicalization_ignores_quoting_only_never_meaning(self) -> None:
        distinct = [
            "systemctl restart nginx",
            "systemctl restart other",
            "rm -rf /a",
            "rm -rf /b",
            "docker ps | grep web",
            "docker ps > /tmp/out",
        ]
        hashes = {opslib.command_hash(cmd) for cmd in distinct}
        self.assertEqual(len(hashes), len(distinct))
        # Token order and operators survive canonicalization.
        self.assertNotEqual(
            opslib.canonical_command("a b c"), opslib.canonical_command("a c b")
        )

    def test_unlexable_text_falls_back_instead_of_raising(self) -> None:
        for command in ("echo 'unbalanced", 'say "half', "", "   "):
            with self.subTest(command=command):
                self.assertEqual(
                    opslib.canonical_command(command), opslib.normalize_command(command)
                )
        # ...and it is still hashable, so the gate never crashes on bad input.
        self.assertTrue(opslib.command_hash("echo 'unbalanced"))

    def test_the_classifier_still_reads_the_raw_command(self) -> None:
        """Canonicalization must not launder shell metacharacters past the
        classifier: `docker ps | rm -rf /` is mutating, quoted or not."""
        verdict = opslib.classify_box_exec_command("docker ps | tee /etc/x")
        self.assertEqual(verdict["verdict"], "mutating")


class MarkerSessionContractTests(unittest.TestCase):
    """`session` is a declared scope, not a field one side happens to write."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        root = Path(self.tmpdir.name)
        for module in (BOX, MCP):
            patch = mock.patch.object(module, "REPO_ROOT", root)
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("SKILLBOX_DRYRUN_MARKER_ROOT", None)
        self.key = BOX.box_exec_marker_key("gatebox", "systemctl restart nginx")

    def _payload(self) -> dict:
        return json.loads(
            BOX._cli_dryrun_marker_path(BOX.BOX_EXEC_MARKER_TOOL, self.key).read_text()  # noqa: SLF001
        )

    def test_cli_markers_declare_session_agnostic_scope(self) -> None:
        BOX.stamp_cli_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, self.key)
        payload = self._payload()
        self.assertEqual(payload["session_scope"], opslib.MARKER_SESSION_SCOPE_ANY)
        self.assertIsNone(payload["session"])
        self.assertEqual(payload["source"], opslib.MARKER_SOURCE_BOX_CLI)

    def test_mcp_markers_declare_session_scope_and_name_the_session(self) -> None:
        with mock.patch.object(MCP, "_dryrun_session_id", return_value="sess-A"):
            MCP._stamp_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, self.key)  # noqa: SLF001
        payload = self._payload()
        self.assertEqual(payload["session_scope"], opslib.MARKER_SESSION_SCOPE_SESSION)
        self.assertEqual(payload["session"], "sess-A")
        self.assertEqual(payload["source"], opslib.MARKER_SOURCE_OPERATOR_MCP)

    def test_a_cli_marker_is_honoured_from_any_mcp_session(self) -> None:
        BOX.stamp_cli_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, self.key)
        for session in ("sess-A", "sess-B"):
            with self.subTest(session=session), \
                 mock.patch.object(MCP, "_dryrun_session_id", return_value=session):
                self.assertTrue(
                    MCP._has_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, self.key)  # noqa: SLF001
                )

    def test_an_mcp_marker_stays_bound_to_its_own_session(self) -> None:
        with mock.patch.object(MCP, "_dryrun_session_id", return_value="sess-A"):
            MCP._stamp_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, self.key)  # noqa: SLF001
            self.assertTrue(
                MCP._has_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, self.key)  # noqa: SLF001
            )
        with mock.patch.object(MCP, "_dryrun_session_id", return_value="sess-B"):
            status = MCP._dryrun_marker_status(  # noqa: SLF001
                BOX.BOX_EXEC_MARKER_TOOL, self.key
            )
        self.assertFalse(status["valid"])
        self.assertEqual(status["reason"], "session-mismatch")
        # ...but the CLI, which has no session identity at all, still honours it.
        self.assertTrue(BOX.cli_dryrun_marker_valid(BOX.BOX_EXEC_MARKER_TOOL, self.key))

    def test_scope_is_read_from_the_marker_not_inferred(self) -> None:
        self.assertEqual(
            opslib.marker_session_scope({"session": "sess-A"}),
            opslib.MARKER_SESSION_SCOPE_SESSION,
        )
        self.assertEqual(opslib.marker_session_scope({}), opslib.MARKER_SESSION_SCOPE_ANY)
        self.assertEqual(
            opslib.marker_session_scope(
                {"session": "sess-A", "session_scope": opslib.MARKER_SESSION_SCOPE_ANY}
            ),
            opslib.MARKER_SESSION_SCOPE_ANY,
        )


class ConsumeOnDispatchTests(_DcgAllowingTestCase):
    """A marker buys ONE attempt: a failed mutating run cannot be replayed."""

    def setUp(self) -> None:
        super().setUp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        root = Path(self.tmpdir.name)
        for module in (BOX, MCP):
            patch = mock.patch.object(module, "REPO_ROOT", root)
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("SKILLBOX_DRYRUN_MARKER_ROOT", None)
        self.command = "systemctl restart nginx"
        self.key = BOX.box_exec_marker_key("gatebox", self.command)
        self.argv = ["exec", "gatebox", "--format", "json", "--", "systemctl", "restart", "nginx"]

    def _run_exec(self, *, ssh_result, argv=None):
        payloads: list[dict] = []
        with mock.patch.object(BOX, "load_inventory", return_value=[_box()]), \
             mock.patch.object(BOX, "_repo_tree_dirty", return_value=""), \
             mock.patch.object(BOX, "ssh_cmd", return_value=ssh_result) as ssh, \
             mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            code = BOX.main(list(argv or self.argv))
        return code, payloads, ssh

    def test_failed_mutating_run_consumes_the_marker_and_refuses_the_retry(self) -> None:
        BOX.stamp_cli_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, self.key)
        code, payloads, ssh = self._run_exec(ssh_result=_completed(1, "", "unit not found"))
        self.assertEqual(code, BOX.EXIT_ERROR)
        ssh.assert_called_once()
        self.assertFalse(
            BOX.cli_dryrun_marker_valid(BOX.BOX_EXEC_MARKER_TOOL, self.key),
            "a failed mutating run left a replayable marker",
        )
        self.assertFalse(
            BOX._cli_dryrun_marker_path(BOX.BOX_EXEC_MARKER_TOOL, self.key).exists()  # noqa: SLF001
        )
        # The immediate retry is refused: it needs a NEW preview.
        code, payloads, ssh = self._run_exec(ssh_result=_completed(0, "ok\n"))
        self.assertNotEqual(code, BOX.EXIT_OK)
        self.assertEqual(payloads[-1]["error"]["type"], "dryrun_marker_required")
        ssh.assert_not_called()

    def test_successful_mutating_run_also_consumes_the_marker(self) -> None:
        BOX.stamp_cli_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, self.key)
        code, _payloads, ssh = self._run_exec(ssh_result=_completed(0, "ok\n"))
        self.assertEqual(code, BOX.EXIT_OK)
        ssh.assert_called_once()
        self.assertFalse(BOX.cli_dryrun_marker_valid(BOX.BOX_EXEC_MARKER_TOOL, self.key))

    def test_a_guard_denial_does_not_spend_the_marker(self) -> None:
        """Nothing was dispatched, so nothing was consumed — the operator may
        fix the command, not be forced to re-preview one that never ran."""
        BOX.stamp_cli_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, self.key)
        deny = {"verdict": "deny", "reason_code": "guard_denied", "reason": "nope",
                "available": True, "fail_closed": False, "decision": "deny",
                "rule_id": "core.test:deny", "binary": "/opt/pinned/dcg",
                "dcg_version": BOX.DCG_PINNED_VERSION,
                "expected_version": BOX.DCG_PINNED_VERSION,
                "interface": BOX.DCG_INTERFACE}
        with mock.patch.object(BOX, "evaluate_command_with_dcg", return_value=deny):
            code, payloads, ssh = self._run_exec(ssh_result=_completed(0, "ok\n"))
        self.assertEqual(code, BOX.EXIT_ERROR)
        self.assertEqual(payloads[-1]["error"]["type"], "dcg_denied")
        ssh.assert_not_called()
        self.assertTrue(BOX.cli_dryrun_marker_valid(BOX.BOX_EXEC_MARKER_TOOL, self.key))

    def test_read_only_runs_never_touch_a_marker(self) -> None:
        BOX.stamp_cli_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, self.key)
        code, _payloads, ssh = self._run_exec(
            ssh_result=_completed(0, "ok\n"),
            argv=["exec", "gatebox", "--format", "json", "--", "docker", "ps"],
        )
        self.assertEqual(code, BOX.EXIT_OK)
        ssh.assert_called_once()
        self.assertTrue(BOX.cli_dryrun_marker_valid(BOX.BOX_EXEC_MARKER_TOOL, self.key))

    def test_failed_compose_down_consumes_the_marker_and_refuses_the_retry(self) -> None:
        BOX.stamp_cli_dryrun_marker(BOX.COMPOSE_DOWN_MARKER_TOOL, BOX.COMPOSE_DOWN_MARKER_KEY)
        payloads: list[dict] = []

        def failing(args, timeout=300):
            return False, 1, {"exit_code": 1, "stderr": "daemon died mid-down"}

        with mock.patch.object(BOX, "_repo_tree_dirty", return_value=""), \
             mock.patch.object(BOX, "run_compose", side_effect=failing) as compose, \
             mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            code = BOX.main(["compose-down", "--format", "json"])
        self.assertEqual(code, BOX.EXIT_ERROR)
        compose.assert_called_once()
        self.assertFalse(
            BOX.cli_dryrun_marker_valid(
                BOX.COMPOSE_DOWN_MARKER_TOOL, BOX.COMPOSE_DOWN_MARKER_KEY
            ),
            "a failed compose-down left a replayable marker",
        )

        payloads.clear()
        with mock.patch.object(BOX, "_repo_tree_dirty", return_value=""), \
             mock.patch.object(BOX, "run_compose") as compose, \
             mock.patch.object(BOX, "emit_json", side_effect=payloads.append):
            code = BOX.main(["compose-down", "--format", "json"])
        self.assertNotEqual(code, BOX.EXIT_OK)
        self.assertEqual(payloads[-1]["error"]["type"], "dryrun_marker_required")
        compose.assert_not_called()

    def test_the_mcp_consumes_on_dispatch_too(self) -> None:
        """Same rule on the other surface, proven through the real store."""
        mcp_box = {"id": "gatebox", "state": "ready", "tailscale_ip": "100.100.0.9",
                   "ssh_user": "skillbox"}
        BOX.stamp_cli_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, self.key)
        with mock.patch.object(MCP, "find_box", return_value=mcp_box), \
             _patch_dcg_allow(MCP), \
             mock.patch.object(MCP, "run_ssh", return_value=(False, 1, {"stderr": "boom"})) as ssh:
            envelope = MCP.handle_operator_box_exec(
                {"box_id": "gatebox", "command": self.command}
            )
        self.assertTrue(envelope["isError"])
        ssh.assert_called_once()
        self.assertFalse(
            MCP._has_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, self.key)  # noqa: SLF001
        )
        with mock.patch.object(MCP, "find_box", return_value=mcp_box), \
             _patch_dcg_allow(MCP), \
             mock.patch.object(MCP, "run_ssh") as ssh:
            envelope = MCP.handle_operator_box_exec(
                {"box_id": "gatebox", "command": self.command}
            )
        self.assertEqual(_mcp_payload(envelope)["error"]["type"], "dry_run_required")
        ssh.assert_not_called()

    def test_a_cli_preview_of_a_quoted_command_authorizes_the_mcp_run(self) -> None:
        """The canonicalization fix, end to end across the two surfaces."""
        argv = ["sh", "-c", "echo hi"]
        cli_command = BOX.box_exec_command_string(argv)
        typed_command = 'sh -c "echo hi"'
        key = BOX.box_exec_marker_key("gatebox", cli_command)
        BOX.stamp_cli_dryrun_marker(BOX.BOX_EXEC_MARKER_TOOL, key)
        mcp_box = {"id": "gatebox", "state": "ready", "tailscale_ip": "100.100.0.9",
                   "ssh_user": "skillbox"}
        with mock.patch.object(MCP, "find_box", return_value=mcp_box), \
             _patch_dcg_allow(MCP), \
             mock.patch.object(MCP, "run_ssh", return_value=(True, 0, {"stdout": "hi"})) as ssh:
            envelope = MCP.handle_operator_box_exec(
                {"box_id": "gatebox", "command": typed_command}
            )
        self.assertFalse(envelope.get("isError"))
        ssh.assert_called_once()
