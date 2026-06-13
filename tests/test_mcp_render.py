from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import mcp_render  # noqa: E402
from runtime_manager.mcp_render import (  # noqa: E402
    collect_mcp_render,
    render_claude_json,
    render_codex_toml,
    render_mcp_sync,
    resolve_codex_cwd,
)
from runtime_manager.machines import (  # noqa: E402
    MachineAlias,
    MachineProfile,
    MachinesConfig,
)


# A canonical declared server map, as selected_mcp_server_configs would return.
DECLARED = {
    "skillbox": {"command": "python3", "args": [".env-manager/mcp_server.py"]},
    "cm": {
        "command": "python3",
        "args": ["scripts/cm_bridge.py", "--port", "3222"],
        "env": {"CASS_PATH": "/x/cass", "CASS_MEMORY_LLM": "none"},
    },
}


def _model(services: list[dict] | None = None) -> dict:
    return {"services": services or [{"id": "cm-mcp", "kind": "mcp"}]}


def _patch_selected(declared: dict, order: list[str] | None = None):
    """Patch selected_mcp_server_configs so the renderer's source of truth is
    the SAME canonical map the audit checks, without needing a runtime .env."""
    order = order or list(declared)
    return mock.patch.object(
        mcp_render,
        "selected_mcp_server_configs",
        return_value=(dict(declared), list(order)),
    )


class RenderSurfaceTests(unittest.TestCase):
    def test_render_claude_json_is_stable_and_valid(self) -> None:
        text = render_claude_json(DECLARED)
        payload = json.loads(text)
        self.assertEqual(sorted(payload["mcpServers"]), ["cm", "skillbox"])
        self.assertTrue(text.endswith("\n"))
        # Stable order so repeated renders are byte-identical.
        self.assertEqual(text, render_claude_json(DECLARED))

    @unittest.skipIf(tomllib is None, "tomllib required")
    def test_render_codex_toml_includes_cwd_and_parses(self) -> None:
        text = render_codex_toml(DECLARED, cwd="/srv/repo")
        doc = tomllib.loads(text)
        self.assertEqual(sorted(doc["mcp_servers"]), ["cm", "skillbox"])
        self.assertEqual(doc["mcp_servers"]["cm"]["cwd"], "/srv/repo")
        self.assertEqual(doc["mcp_servers"]["cm"]["env"]["CASS_PATH"], "/x/cass")
        # cwd is rendered for every server so Codex launches in the right place.
        self.assertEqual(doc["mcp_servers"]["skillbox"]["cwd"], "/srv/repo")

    @unittest.skipIf(tomllib is None, "tomllib required")
    def test_codex_toml_preserves_non_mcp_preamble(self) -> None:
        preamble = textwrap.dedent(
            """\
            #:schema https://example/schema.json

            [features]
            apps = false
            """
        ).strip()
        text = render_codex_toml(DECLARED, cwd="/srv/repo", preamble=preamble)
        doc = tomllib.loads(text)
        self.assertEqual(doc["features"]["apps"], False)
        self.assertIn("#:schema", text)


class MachineProfilePathTests(unittest.TestCase):
    def _config(self) -> MachinesConfig:
        devbox = MachineProfile(
            machine_id="devbox",
            hostnames=("devbox",),
            repo_roots=("/srv/skillbox/repos",),
        )
        mac = MachineProfile(
            machine_id="mac",
            hostnames=("mac",),
            repo_roots=("/Users/b/repos",),
        )
        return MachinesConfig(
            machines={"devbox": devbox, "mac": mac},
            aliases=(MachineAlias(alias="/srv/repos", canonical="/srv/skillbox/repos"),),
        )

    def test_foreign_mac_path_translates_onto_devbox_root(self) -> None:
        config = self._config()
        devbox = config.get("devbox")
        # A /Users/b path handed to a devbox must NOT survive into config.
        resolved = resolve_codex_cwd(
            Path("/Users/b/repos/opensource/skillbox"),
            machines=config,
            profile=devbox,
        )
        self.assertEqual(resolved, "/srv/skillbox/repos/opensource/skillbox")
        self.assertNotIn("/Users/b", resolved)

    def test_local_devbox_path_is_unchanged(self) -> None:
        config = self._config()
        devbox = config.get("devbox")
        resolved = resolve_codex_cwd(
            Path("/srv/skillbox/repos/opensource/skillbox"),
            machines=config,
            profile=devbox,
        )
        self.assertEqual(resolved, "/srv/skillbox/repos/opensource/skillbox")

    def test_alias_path_canonicalizes_for_local_machine(self) -> None:
        config = self._config()
        devbox = config.get("devbox")
        resolved = resolve_codex_cwd(
            Path("/srv/repos/opensource/skillbox"),
            machines=config,
            profile=devbox,
        )
        self.assertEqual(resolved, "/srv/skillbox/repos/opensource/skillbox")

    def test_unknown_path_with_no_machines_is_returned_as_is(self) -> None:
        # No machines.yaml -> the resolved absolute path is used unchanged.
        with mock.patch.object(
            mcp_render.machines_mod,
            "load_machines_config",
            side_effect=mcp_render.machines_mod.MachinesConfigError("no file"),
        ):
            resolved = resolve_codex_cwd(Path("/some/repo"))
        self.assertEqual(resolved, "/some/repo")


class CollectRenderTests(unittest.TestCase):
    def _setup_repo(self, tmp: str) -> tuple[Path, Path]:
        root = Path(tmp) / "skillbox"
        root.mkdir()
        target = Path(tmp) / "repo"
        target.mkdir()
        (target / ".git").mkdir()
        return root, target

    def test_collect_render_produces_both_surfaces_from_one_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _patch_selected(DECLARED):
            root, target = self._setup_repo(tmp)
            payload = collect_mcp_render(root, _model(), config_root=str(target))
        self.assertEqual(payload["declared_servers"], ["skillbox", "cm"])
        self.assertTrue(payload["surfaces"]["claude"]["changed"])
        self.assertTrue(payload["surfaces"]["codex"]["changed"])
        # Both surfaces render the SAME declared server set.
        self.assertEqual(payload["surfaces"]["claude"]["servers"], ["cm", "skillbox"])
        self.assertEqual(payload["surfaces"]["codex"]["servers"], ["cm", "skillbox"])

    def test_unmanaged_existing_entries_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _patch_selected(DECLARED):
            root, target = self._setup_repo(tmp)
            # Operator added an extra server not in the declaration.
            (target / ".mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "skillbox": {"command": "old"},
                            "operator-extra": {"command": "custom", "args": ["x"]},
                        }
                    }
                ),
                encoding="utf-8",
            )
            payload = collect_mcp_render(root, _model(), config_root=str(target))
        claude = payload["surfaces"]["claude"]
        # The undeclared operator entry survives; declared entries regenerate.
        self.assertIn("operator-extra", claude["servers"])
        self.assertEqual(claude["provenance"]["operator-extra"], "preserved")
        self.assertEqual(claude["provenance"]["skillbox"], "managed")
        rendered = json.loads(claude["rendered"])
        self.assertEqual(
            rendered["mcpServers"]["operator-extra"], {"command": "custom", "args": ["x"]}
        )
        self.assertIn("operator-extra", payload["summary"]["preserved"])

    def test_operator_managed_declared_entry_is_left_untouched(self) -> None:
        model = _model([{"id": "cm-mcp", "kind": "mcp", "operator_managed": True}])
        with tempfile.TemporaryDirectory() as tmp, _patch_selected(DECLARED):
            root, target = self._setup_repo(tmp)
            (target / ".mcp.json").write_text(
                json.dumps(
                    {"mcpServers": {"cm": {"command": "operator-wrote-this"}}}
                ),
                encoding="utf-8",
            )
            payload = collect_mcp_render(root, model, config_root=str(target))
        claude = payload["surfaces"]["claude"]
        self.assertEqual(claude["provenance"]["cm"], "operator-managed")
        rendered = json.loads(claude["rendered"])
        # The operator's body wins over the regenerated declaration body.
        self.assertEqual(rendered["mcpServers"]["cm"], {"command": "operator-wrote-this"})


class SyncSymmetryTests(unittest.TestCase):
    def test_dry_run_matches_apply_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _patch_selected(DECLARED):
            root = Path(tmp) / "skillbox"
            root.mkdir()
            target = Path(tmp) / "repo"
            target.mkdir()
            (target / ".git").mkdir()

            dry = render_mcp_sync(root, _model(), config_root=str(target), apply=False)
            self.assertTrue(dry["dry_run"])
            self.assertFalse(dry["applied"])
            self.assertEqual(dry["written"], [])
            # Nothing was written on dry-run.
            self.assertFalse((target / ".mcp.json").exists())
            self.assertFalse((target / ".codex" / "config.toml").exists())

            applied = render_mcp_sync(root, _model(), config_root=str(target), apply=True)
            self.assertTrue(applied["applied"])
            self.assertEqual(len(applied["written"]), 2)

            # The plan text is identical between dry-run and apply...
            for key in ("claude", "codex"):
                self.assertEqual(
                    dry["surfaces"][key]["rendered"],
                    applied["surfaces"][key]["rendered"],
                    f"{key}: dry-run and apply must render identical text",
                )
            # ...and what apply wrote to disk equals that rendered text.
            self.assertEqual(
                (target / ".mcp.json").read_text(encoding="utf-8"),
                dry["surfaces"]["claude"]["rendered"],
            )
            self.assertEqual(
                (target / ".codex" / "config.toml").read_text(encoding="utf-8"),
                dry["surfaces"]["codex"]["rendered"],
            )

    def test_apply_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _patch_selected(DECLARED):
            root = Path(tmp) / "skillbox"
            root.mkdir()
            target = Path(tmp) / "repo"
            target.mkdir()
            (target / ".git").mkdir()

            render_mcp_sync(root, _model(), config_root=str(target), apply=True)
            second = render_mcp_sync(root, _model(), config_root=str(target), apply=False)
        # A re-render after apply reports no changes.
        self.assertFalse(second["summary"]["claude_changed"])
        self.assertFalse(second["summary"]["codex_changed"])


class CliWiringTests(unittest.TestCase):
    def test_mcp_sync_registered_in_dispatch_and_names(self) -> None:
        from runtime_manager import cli

        self.assertIn("mcp", cli._MODEL_DISPATCH)
        self.assertIn("mcp", cli.MANAGE_COMMAND_NAMES)

    def test_command_registry_has_mcp_sync_spec(self) -> None:
        from runtime_manager.command_registry import default_registry, validate_registry

        specs = list(default_registry())
        ids = {spec.id for spec in specs}
        self.assertIn("runtime.mcp_sync", ids)
        # The whole registry (including the new spec) must still validate.
        self.assertEqual(validate_registry(specs), [])


if __name__ == "__main__":
    unittest.main()
