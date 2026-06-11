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

    def test_declared_profile_gated_servers_are_intentional_not_drift(self) -> None:
        """Servers declared as kind:mcp services (under any profile) that appear
        only in the Claude surface are a deliberate status, not unexplained drift."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".git").mkdir()
            (root / ".codex").mkdir()
            (root / ".mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "skillbox": {"command": "python3"},
                            "fwc": {"command": "fwc"},
                            "dcg": {"command": "dcg"},
                            "rogue": {"command": "custom"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / ".codex" / "config.toml").write_text(
                "[mcp_servers.skillbox]\ncommand = \"python3\"\n",
                encoding="utf-8",
            )

            # Active model only declares skillbox; fwc/dcg live behind inactive
            # profiles but are declared in the full model passed via declared_servers.
            payload = collect_mcp_audit(
                root,
                {"services": []},
                config_root=str(root),
                declared_servers=["skillbox", "fwc", "dcg"],
            )

        claude = payload["surfaces"]["claude"]
        # fwc/dcg are declared -> intentional; rogue is undeclared -> drift.
        self.assertEqual(claude["unexpected"], ["rogue"])
        self.assertEqual(claude["extra_intentional"], ["dcg", "fwc"])
        self.assertEqual(payload["parity"]["claude_only_declared"], ["dcg", "fwc"])
        self.assertEqual(payload["parity"]["claude_only_unexpected"], ["rogue"])
        self.assertEqual(payload["summary"]["unexplained_drift"], 1)
        # Only the undeclared server is nagged about; declared ones are silent.
        joined = " ".join(payload["next_actions"])
        self.assertIn("rogue", joined)
        self.assertNotIn("fwc", joined)
        self.assertNotIn("dcg", joined)

    def test_declared_servers_ignored_when_auditing_foreign_repo(self) -> None:
        """A foreign --cwd must not borrow this repo's declarations to excuse drift."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "skillbox"
            root.mkdir()
            (root / ".git").mkdir()
            repo = Path(tmpdir) / "other"
            repo.mkdir()
            (repo / ".git").mkdir()
            (repo / ".codex").mkdir()
            (repo / ".mcp.json").write_text(
                json.dumps({"mcpServers": {"skillbox": {"command": "python3"}, "fwc": {"command": "fwc"}}}),
                encoding="utf-8",
            )
            (repo / ".codex" / "config.toml").write_text(
                "[mcp_servers.skillbox]\ncommand = \"python3\"\n",
                encoding="utf-8",
            )

            payload = collect_mcp_audit(
                root,
                {"services": []},
                cwd=str(repo),
                declared_servers=["skillbox", "fwc"],
            )

        # declared_servers belongs to root (skillbox), not the foreign repo, so fwc
        # is still surfaced as unexplained drift there.
        self.assertEqual(payload["surfaces"]["claude"]["unexpected"], ["fwc"])
        self.assertEqual(payload["summary"]["unexplained_drift"], 1)

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

    def test_broken_symlink_config_is_surfaced_not_just_absent(self) -> None:
        """A dangling config symlink (e.g. after a host migration) must be
        reported as a broken link to repair, not as a missing file to write
        through; writing through the dangling link fails or mis-targets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".git").mkdir()
            (root / ".mcp.json").write_text(
                json.dumps({"mcpServers": {"skillbox": {"command": "python3"}}}),
                encoding="utf-8",
            )
            (root / ".codex").mkdir()
            dangling_target = root / "gone" / "skillbox.toml"
            (root / ".codex" / "config.toml").symlink_to(dangling_target)

            payload = collect_mcp_audit(root, {"services": []}, config_root=str(root))
            stdout = StringIO()
            with redirect_stdout(stdout):
                print_mcp_audit_text(payload, root_dir=root)

        codex = payload["surfaces"]["codex"]
        self.assertFalse(codex["present"])
        self.assertTrue(codex["broken_symlink"])
        self.assertEqual(codex["symlink_target"], str(dangling_target))
        # Regular-file surfaces keep the same stable keys.
        claude = payload["surfaces"]["claude"]
        self.assertFalse(claude["broken_symlink"])
        self.assertIsNone(claude["symlink_target"])
        # The repair action replaces the misleading "add servers" action.
        actions = payload["next_actions"]
        self.assertTrue(any("repair broken symlink" in action for action in actions))
        self.assertFalse(
            any(action.startswith("add ") and "config.toml" in action for action in actions)
        )
        self.assertIn("symlink: broken ->", stdout.getvalue())

    def test_working_symlink_config_is_present_and_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".git").mkdir()
            real_config = root / "real-config.toml"
            real_config.write_text(
                "[mcp_servers.skillbox]\ncommand = \"python3\"\n",
                encoding="utf-8",
            )
            (root / ".codex").mkdir()
            (root / ".codex" / "config.toml").symlink_to(real_config)

            payload = collect_mcp_audit(root, {"services": []}, config_root=str(root))

        codex = payload["surfaces"]["codex"]
        self.assertTrue(codex["present"])
        self.assertFalse(codex["broken_symlink"])
        self.assertEqual(codex["symlink_target"], str(real_config))
        self.assertIn("skillbox", codex["effective_servers"])

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
