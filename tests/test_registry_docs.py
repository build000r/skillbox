from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import command_registry as REG  # noqa: E402
from runtime_manager import registry_docs as DOCS  # noqa: E402


REGEN_ENV = "REGEN_REGISTRY_DOCS"


def _run_manage(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, ".env-manager/manage.py", *args],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ENV_MANAGER_DIR)},
    )


class RegistryDocsTests(unittest.TestCase):
    maxDiff = None
    _regen = bool(os.environ.get(REGEN_ENV))

    def test_committed_reference_matches_renderer(self) -> None:
        rendered = DOCS.render_api_reference()
        path = ROOT_DIR / DOCS.API_REFERENCE_RELATIVE_PATH
        if self._regen:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
        self.assertTrue(
            path.is_file(),
            f"Missing generated API reference {path}. Regenerate with "
            f"{REGEN_ENV}=1 python3 -m unittest tests.test_registry_docs",
        )
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            rendered,
            "docs/API_REFERENCE.md is stale vs runtime_manager.registry_docs. "
            f"Regenerate with {REGEN_ENV}=1 python3 -m unittest tests.test_registry_docs "
            "after intentional registry or renderer changes.",
        )

    def test_renderer_is_deterministic(self) -> None:
        self.assertEqual(DOCS.render_api_reference(), DOCS.render_api_reference())

    def test_reference_covers_every_registry_entry_once(self) -> None:
        rendered = DOCS.render_api_reference()
        specs = REG.default_registry()
        self.assertEqual(rendered.count("\n#### "), len(specs))
        for spec in specs:
            self.assertEqual(rendered.count(f"#### {spec.id}\n"), 1, spec.id)
        self.assertIn("#### runtime.registry_docs", rendered)

    def test_reference_carries_generated_abi_banner(self) -> None:
        rendered = DOCS.render_api_reference()
        self.assertIn("GENERATED FILE", rendered)
        self.assertIn("Do not edit by hand", rendered)
        self.assertIn(REG.REGISTRY_ABI_VERSION, rendered)

    def test_representative_brain_next_section_has_contract_details(self) -> None:
        rendered = DOCS.render_api_reference()
        start = rendered.index("#### brain.next\n")
        end = rendered.index("\n#### brain.search\n", start)
        section = rendered[start:end]
        self.assertIn("Rank the highest-leverage next actions", section)
        self.assertIn("- Surfaces: `cli`, `mcp`", section)
        self.assertIn("- Side effect: `none`", section)
        self.assertIn("- Risk: `low`", section)
        self.assertIn("- MCP mirror: `skillbox_next`", section)
        self.assertIn("| `limit` | `integer?` | no |", section)
        self.assertIn("| `recommendations` | `object[]` | yes |", section)
        self.assertIn("python3 .env-manager/manage.py next --client skillbox --format json --limit 5", section)
        self.assertIn("python3 -m unittest tests.test_agent_ops_command_registry", section)

    def test_cli_registry_docs_outputs_md_and_compact_json(self) -> None:
        md = _run_manage("registry-docs", "--format", "md")
        self.assertEqual(md.returncode, 0, md.stderr)
        self.assertEqual(md.stdout, DOCS.render_api_reference())

        result = _run_manage("registry-docs", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["abi_version"], REG.REGISTRY_ABI_VERSION)
        self.assertEqual(payload["count"], len(REG.default_registry()))
        self.assertEqual(payload["path"], str(DOCS.API_REFERENCE_RELATIVE_PATH))
        self.assertEqual(payload["written"], False)
        self.assertNotIn("content", payload)

    def test_capabilities_lists_registry_docs_safe_first_try(self) -> None:
        result = _run_manage("capabilities", "--format", "json", "--no-adapters")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        commands = {command["name"]: command for command in payload["commands"]}
        self.assertIn("registry-docs", commands)
        self.assertEqual(
            commands["registry-docs"]["safe_first_try"],
            "manage.py registry-docs --format md",
        )
        registry = {entry["id"]: entry for entry in payload["registry"]["capabilities"]}
        self.assertIn("runtime.registry_docs", registry)
        self.assertEqual(registry["runtime.registry_docs"]["side_effect"], "local_write")
        self.assertEqual(registry["runtime.registry_docs"]["risk"], "low")


if __name__ == "__main__":
    unittest.main()
