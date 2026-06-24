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
    USER_GLOBAL_CODEX_REL,
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


class CodexBodyFidelityTests(unittest.TestCase):
    """BUG 1 — render_codex_toml must re-emit each server body FAITHFULLY.

    Previously only command/args/cwd/env were emitted and cwd was ALWAYS
    injected, so other keys (startup_timeout_ms, and for http servers
    type/url/headers/bearer_token_env_var) were dropped and an operator's own
    cwd was clobbered with the machine cwd.
    """

    @unittest.skipIf(tomllib is None, "tomllib required")
    def test_extra_scalar_keys_and_http_keys_survive(self) -> None:
        servers = {
            # http-type server: no command, must keep type/url/headers/bearer
            # and must NOT get a bogus machine cwd forced onto it.
            "uidotsh": {
                "type": "http",
                "url": "https://ui.sh/mcp?agent=codex",
                "bearer_token_env_var": "UIDOTSH_TOKEN",
                "startup_timeout_ms": 30000,
                "headers": {"Authorization": "Bearer x"},
                "tools": {"uidotsh_fetch": {"approval_mode": "approve"}},
            },
        }
        text = render_codex_toml(servers, cwd="/machine/cwd")
        u = tomllib.loads(text)["mcp_servers"]["uidotsh"]
        self.assertEqual(u["type"], "http")
        self.assertEqual(u["url"], "https://ui.sh/mcp?agent=codex")
        self.assertEqual(u["bearer_token_env_var"], "UIDOTSH_TOKEN")
        self.assertEqual(u["startup_timeout_ms"], 30000)
        self.assertEqual(u["headers"], {"Authorization": "Bearer x"})
        # Nested dict-of-dict subtable survives (not just env).
        self.assertEqual(u["tools"], {"uidotsh_fetch": {"approval_mode": "approve"}})
        # A remote/http server (no command) is never bricked with a cwd.
        self.assertNotIn("cwd", u)

    @unittest.skipIf(tomllib is None, "tomllib required")
    def test_operator_cwd_wins_over_machine_cwd(self) -> None:
        servers = {
            "cm": {
                "command": "python3",
                "args": ["x.py"],
                "cwd": "/operator/own/cwd",
                "startup_timeout_ms": 5000,
                "env": {"A": "1"},
            },
            "skillbox": {"command": "python3", "args": ["srv.py"]},
        }
        doc = tomllib.loads(render_codex_toml(servers, cwd="/machine/cwd"))
        cm = doc["mcp_servers"]["cm"]
        # Operator's explicit cwd is NOT overwritten with the machine cwd.
        self.assertEqual(cm["cwd"], "/operator/own/cwd")
        self.assertEqual(cm["startup_timeout_ms"], 5000)
        self.assertEqual(cm["env"]["A"], "1")
        # A command server with no cwd still gets the machine cwd injected.
        self.assertEqual(doc["mcp_servers"]["skillbox"]["cwd"], "/machine/cwd")

    @unittest.skipIf(tomllib is None, "tomllib required")
    def test_faithful_body_render_is_byte_symmetric(self) -> None:
        servers = {
            "cm": {
                "command": "python3",
                "args": ["x.py"],
                "cwd": "/op",
                "startup_timeout_ms": 5000,
                "env": {"B": "2", "A": "1"},
            },
        }
        a = render_codex_toml(servers, cwd="/machine/cwd")
        b = render_codex_toml(servers, cwd="/machine/cwd")
        self.assertEqual(a, b)


class CodexTopLevelScalarTests(unittest.TestCase):
    """BUG 2 — preserved top-level SCALAR keys must survive a sync.

    _emit_toml_section only emitted dict values, so top-level scalars
    (model/approval_policy/sandbox_mode/cli_auth_credentials_store) were
    silently dropped from preserved sections on every render.
    """

    @unittest.skipIf(tomllib is None, "tomllib required")
    def test_mixed_top_level_scalars_and_tables_round_trip(self) -> None:
        raw = textwrap.dedent(
            """\
            #:schema https://developers.openai.com/codex/config-schema.json
            cli_auth_credentials_store = "file"
            approval_policy = "never"
            sandbox_mode = "danger-full-access"
            model = "gpt-5.5"
            model_reasoning_effort = "xhigh"

            [features]
            apps = false
            goals = true

            [projects."/srv/repos/cfo"]
            trust_level = "trusted"
            """
        )
        doc = tomllib.loads(raw)
        preamble = mcp_render._codex_preamble(doc, raw)
        text = render_codex_toml(DECLARED, cwd="/srv/repo", preamble=preamble)
        out = tomllib.loads(text)
        # Top-level scalars survive losslessly.
        self.assertEqual(out["model"], "gpt-5.5")
        self.assertEqual(out["approval_policy"], "never")
        self.assertEqual(out["sandbox_mode"], "danger-full-access")
        self.assertEqual(out["cli_auth_credentials_store"], "file")
        self.assertEqual(out["model_reasoning_effort"], "xhigh")
        # Tables (and the schema comment) still survive alongside the scalars.
        self.assertEqual(out["features"], {"apps": False, "goals": True})
        self.assertEqual(out["projects"]["/srv/repos/cfo"]["trust_level"], "trusted")
        self.assertIn("#:schema", text)
        # In TOML, top-level scalars must precede any [table] header.
        first_table = text.index("[features]")
        self.assertLess(text.index('model = "gpt-5.5"'), first_table)


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
            repo_roots=("/Users/operator/repos",),
        )
        return MachinesConfig(
            machines={"devbox": devbox, "mac": mac},
            aliases=(MachineAlias(alias="/srv/repos", canonical="/srv/skillbox/repos"),),
        )

    def test_foreign_mac_path_translates_onto_devbox_root(self) -> None:
        config = self._config()
        devbox = config.get("devbox")
        # A /Users/operator path handed to a devbox must NOT survive into config.
        resolved = resolve_codex_cwd(
            Path("/Users/operator/repos/opensource/skillbox"),
            machines=config,
            profile=devbox,
        )
        self.assertEqual(resolved, "/srv/skillbox/repos/opensource/skillbox")
        self.assertNotIn("/Users/operator", resolved)

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


class PreservedCodexServerSurvivesSyncTests(unittest.TestCase):
    """BUG 1 (end-to-end) — a preserved operator Codex server with extra keys,
    http-type keys, and its own cwd survives a full ``mcp sync --apply``
    byte-faithfully, and dry-run stays symmetric with apply."""

    @unittest.skipIf(tomllib is None, "tomllib required")
    def test_preserved_http_and_extra_keys_and_operator_cwd_survive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _patch_selected(DECLARED):
            root = Path(tmp) / "skillbox"
            root.mkdir()
            target = Path(tmp) / "repo"
            target.mkdir()
            (target / ".git").mkdir()
            codex_dir = target / ".codex"
            codex_dir.mkdir()
            # Operator-authored config.toml: top-level scalars + an http server
            # (undeclared, so preserved) + a declared server (cm) the operator
            # gave an explicit cwd and a startup_timeout_ms.
            (codex_dir / "config.toml").write_text(
                textwrap.dedent(
                    """\
                    #:schema https://developers.openai.com/codex/config-schema.json
                    approval_policy = "never"
                    model = "gpt-5.5"

                    [mcp_servers.uidotsh]
                    type = "http"
                    url = "https://ui.sh/mcp?agent=codex"
                    bearer_token_env_var = "UIDOTSH_TOKEN"
                    startup_timeout_ms = 30000

                    [mcp_servers.uidotsh.headers]
                    Authorization = "Bearer x"

                    [mcp_servers.operator-extra]
                    command = "python3"
                    args = ["scripts/operator.py"]
                    cwd = "/operator/own/cwd"
                    startup_timeout_ms = 5000

                    [mcp_servers.operator-extra.env]
                    OP_KEY = "op-value"
                    """
                ),
                encoding="utf-8",
            )

            dry = render_mcp_sync(root, _model(), config_root=str(target), apply=False)
            applied = render_mcp_sync(root, _model(), config_root=str(target), apply=True)

        # Dry-run and apply render identical Codex text (symmetry preserved).
        self.assertEqual(
            dry["surfaces"]["codex"]["rendered"],
            applied["surfaces"]["codex"]["rendered"],
        )
        out = tomllib.loads(applied["surfaces"]["codex"]["rendered"])

        # Top-level scalars survived (BUG 2 in the live path too).
        self.assertEqual(out["model"], "gpt-5.5")
        self.assertEqual(out["approval_policy"], "never")

        # The undeclared http server is preserved verbatim, NOT bricked.
        u = out["mcp_servers"]["uidotsh"]
        self.assertEqual(u["type"], "http")
        self.assertEqual(u["url"], "https://ui.sh/mcp?agent=codex")
        self.assertEqual(u["bearer_token_env_var"], "UIDOTSH_TOKEN")
        self.assertEqual(u["startup_timeout_ms"], 30000)
        self.assertEqual(u["headers"], {"Authorization": "Bearer x"})
        self.assertNotIn("cwd", u)  # no bogus cwd on a remote server

        # The undeclared (preserved) operator server: its OWN cwd survives the
        # sync (machine cwd never clobbers it), plus extra key + env intact.
        op = out["mcp_servers"]["operator-extra"]
        self.assertEqual(op["cwd"], "/operator/own/cwd")
        self.assertEqual(op["startup_timeout_ms"], 5000)
        self.assertEqual(op["env"]["OP_KEY"], "op-value")
        self.assertEqual(applied["surfaces"]["codex"]["provenance"]["operator-extra"], "preserved")

        # cm (declared, managed: declaration body wins) and skillbox both get
        # the machine cwd injected since their declared bodies carry no cwd.
        self.assertIn("cwd", out["mcp_servers"]["cm"])
        self.assertIn("cwd", out["mcp_servers"]["skillbox"])


class UserGlobalCodexRefusalTests(unittest.TestCase):
    """BUG 3 — ``mcp sync`` must REFUSE to write the operator's global
    ``~/.codex/config.toml`` (resolved when ``--cwd ~`` has no .git ancestor)."""

    def test_sync_refuses_to_write_user_global_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _patch_selected(DECLARED):
            fake_home = Path(tmp) / "home"
            (fake_home / ".codex").mkdir(parents=True)
            global_codex = fake_home / USER_GLOBAL_CODEX_REL
            original = 'model = "gpt-5.5"\napproval_policy = "never"\n'
            global_codex.write_text(original, encoding="utf-8")

            root = Path(tmp) / "skillbox"
            root.mkdir()

            with mock.patch.object(
                mcp_render.Path, "home", return_value=fake_home
            ):
                # cwd == home, no .git above home -> Codex target resolves to
                # exactly ~/.codex/config.toml.
                payload = render_mcp_sync(
                    root, _model(), cwd=str(fake_home), apply=True
                )

            # The operator's global Codex config was NOT touched.
            self.assertEqual(global_codex.read_text(encoding="utf-8"), original)

        codex_surface = payload["surfaces"]["codex"]
        self.assertTrue(codex_surface["refused"])
        self.assertIn("~/.codex/config.toml", codex_surface["refused_reason"])
        # A refused surface is reported as not-changed so apply skips it...
        self.assertFalse(codex_surface["changed"])
        # ...and is never in the written list, but is in the refused list.
        self.assertNotIn(str(global_codex), payload["written"])
        self.assertIn(str(global_codex), payload["refused"])

    def test_repo_local_codex_is_not_refused(self) -> None:
        # A normal repo-rooted target (.git present) is NOT the user-global
        # path, so it writes as usual.
        with tempfile.TemporaryDirectory() as tmp, _patch_selected(DECLARED):
            root = Path(tmp) / "skillbox"
            root.mkdir()
            target = Path(tmp) / "repo"
            target.mkdir()
            (target / ".git").mkdir()
            payload = render_mcp_sync(root, _model(), config_root=str(target), apply=True)
        self.assertFalse(payload["surfaces"]["codex"]["refused"])
        self.assertEqual(payload["refused"], [])
        self.assertIn(str(target / ".codex" / "config.toml"), payload["written"])


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
