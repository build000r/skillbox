from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.mcp_visibility import collect_mcp_audit, print_mcp_audit_text  # noqa: E402


class McpVisibilityTests(unittest.TestCase):
    def test_collect_mcp_audit_compares_claude_json_and_codex_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skillbox"
            root.mkdir()
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            (repo / ".codex").mkdir()
            (repo / ".mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "skillbox": {"command": "python3"},
                            "cm": {"command": "cm"},
                            "claude-only": {"command": "custom"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            (repo / ".codex" / "config.toml").write_text(
                textwrap.dedent(
                    """\
                    [mcp_servers.skillbox]
                    command = "python3"

                    [mcp_servers."codex-only"]
                    command = "custom"

                    [mcp_servers.disabled]
                    command = "custom"
                    enabled = false
                    """
                ),
                encoding="utf-8",
            )

            payload = collect_mcp_audit(
                root,
                {"services": [{"id": "cm-mcp", "kind": "mcp"}]},
                cwd=str(repo / "src"),
            )

        self.assertEqual(payload["expected_servers"], ["cm", "skillbox"])
        self.assertEqual(payload["config_root"], str(repo.resolve()))
        self.assertEqual(payload["surfaces"]["claude"]["extra"], ["claude-only"])
        self.assertEqual(payload["surfaces"]["codex"]["missing"], ["cm"])
        self.assertEqual(payload["surfaces"]["codex"]["extra"], ["codex-only"])
        self.assertEqual(payload["surfaces"]["codex"]["disabled_servers"], ["disabled"])
        self.assertEqual(payload["parity"]["claude_only"], ["claude-only", "cm"])
        self.assertEqual(payload["parity"]["codex_only"], ["codex-only"])
        self.assertTrue(any("add cm" in action for action in payload["next_actions"]))

    def test_collect_mcp_audit_reports_invalid_configs_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".mcp.json").write_text("{bad json", encoding="utf-8")
            (root / ".codex").mkdir()
            (root / ".codex" / "config.toml").write_text("[mcp_servers", encoding="utf-8")

            payload = collect_mcp_audit(root, {"services": []}, config_root=str(root))

        self.assertFalse(payload["surfaces"]["claude"]["valid"])
        self.assertFalse(payload["surfaces"]["codex"]["valid"])
        self.assertEqual(payload["summary"]["invalid_configs"], 2)
        self.assertTrue(any("fix" in action for action in payload["next_actions"]))

    def test_print_mcp_audit_text_is_compact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            payload = collect_mcp_audit(root, {"services": []}, config_root=str(root))
            stdout = StringIO()
            with redirect_stdout(stdout):
                print_mcp_audit_text(payload, root_dir=root)

        text = stdout.getvalue()
        self.assertIn("mcp audit:", text)
        self.assertIn("claude-json:", text)
        self.assertIn("codex-toml:", text)
        self.assertIn("expected:", text)


if __name__ == "__main__":
    unittest.main()
