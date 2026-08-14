"""vniq.4: operator MCP deprecation — CLI parity, pointers, and the operator skill.

The skillbox-operator MCP server is being retired in favour of the robot CLI.
Retirement is only honest if an agent that never sees the MCP server can still do
everything the MCP server could, with the same gates. These tests pin the three
things that make that true, so none of them can silently rot:

- EVERY MCP tool declares a CLI replacement, and that replacement is a real
  box.py verb (or an explicitly recorded non-box.py entrypoint). A new MCP tool
  without a CLI equivalent fails here.
- box.py no longer points agents AT the MCP server for anything it can do
  itself — the capabilities payload and robot docs route remote commands through
  `box.py exec`, and record the deprecation plus the known gaps.
- the replacement skill exists and actually carries the gating story (dry-run
  first, marker semantics, DCG fail-closed, confirm before destructive), because
  a skill that lists verbs without the gates would migrate agents onto the CLI
  while dropping the safety posture that justified the migration.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BOX_SCRIPT = ROOT_DIR / "scripts" / "box.py"
MCP_SCRIPT = ROOT_DIR / "scripts" / "operator_mcp_server.py"
SKILL_DIR = ROOT_DIR / "skills" / "box-fleet-operator"
SKILL_FILE = SKILL_DIR / "SKILL.md"
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

_spec = importlib.util.spec_from_file_location("deprecation_test_box", BOX_SCRIPT)
assert _spec and _spec.loader
BOX = importlib.util.module_from_spec(_spec)
sys.modules["deprecation_test_box"] = BOX
_spec.loader.exec_module(BOX)

MCP = SourceFileLoader("deprecation_test_mcp", str(MCP_SCRIPT.resolve())).load_module()

BOX_CLI_PREFIX = "python3 scripts/box.py "

# Tools whose replacement is deliberately NOT a box.py verb. Each entry is a
# standing debt or an accepted boundary; adding one requires justifying it here.
NON_BOX_REPLACEMENTS = {
    # Accepted boundary, NOT a gap: outer validation has always lived in the
    # reconcile script, and the MCP tools only shelled out to it.
    "operator_doctor": "python3 scripts/04-reconcile.py doctor --format json",
    "operator_render": "python3 scripts/04-reconcile.py render --format json",
}


def _contract(tool: dict) -> dict:
    return tool["x_skillbox_contract"]


def _cli_verb(replacement: str) -> str | None:
    """The box.py verb a replacement invokes, or None if it is not a box.py call."""
    if not replacement.startswith(BOX_CLI_PREFIX):
        return None
    return replacement[len(BOX_CLI_PREFIX):].split()[0]


class ToolParityTests(unittest.TestCase):
    """Every MCP tool has a usable, real CLI replacement."""

    def test_every_tool_is_marked_deprecated_with_a_cli_replacement(self) -> None:
        self.assertTrue(MCP.DEPRECATED)
        for tool in MCP.TOOLS:
            with self.subTest(tool=tool["name"]):
                contract = _contract(tool)
                self.assertTrue(contract["deprecated"])
                self.assertTrue(contract["cli_replacement"].strip())
                self.assertEqual(contract["cli_replacement"], contract["exact_cli"])

    def test_every_tool_description_names_its_replacement(self) -> None:
        """An agent reading tools/list must see where to go without extra lookups."""
        for tool in MCP.TOOLS:
            with self.subTest(tool=tool["name"]):
                description = tool["description"]
                self.assertIn("DEPRECATED", description)
                self.assertIn(_contract(tool)["cli_replacement"], description)
                self.assertIn(MCP.DEPRECATION_REPLACEMENT_SKILL, description)

    def test_box_py_replacements_name_real_verbs(self) -> None:
        """A replacement pointing at a verb box.py does not have is worse than none."""
        checked = 0
        for tool in MCP.TOOLS:
            verb = _cli_verb(_contract(tool)["cli_replacement"])
            if verb is None:
                continue
            checked += 1
            with self.subTest(tool=tool["name"], verb=verb):
                self.assertIn(verb, BOX.BOX_COMMAND_NAMES)
                self.assertIn(verb, BOX.BOX_JSON_COMMANDS)
        self.assertGreaterEqual(checked, 7)

    def test_only_the_recorded_tools_lack_a_box_py_equivalent(self) -> None:
        """Pins the parity GAP. A new gap must be argued for here, not drift in."""
        gaps = {
            tool["name"]: _contract(tool)["cli_replacement"]
            for tool in MCP.TOOLS
            if _cli_verb(_contract(tool)["cli_replacement"]) is None
        }
        self.assertEqual(gaps, NON_BOX_REPLACEMENTS)

    def test_mutating_tools_replace_with_a_dry_run_first_command(self) -> None:
        """The advertised CLI entrypoint for a destructive tool is the preview."""
        for tool in MCP.TOOLS:
            contract = _contract(tool)
            if not contract["dry_run_required"]:
                continue
            with self.subTest(tool=tool["name"]):
                self.assertIn("--dry-run", contract["cli_replacement"])

    def test_server_instructions_lead_with_the_deprecation(self) -> None:
        instructions = MCP.handle_initialize({})["instructions"]
        self.assertTrue(instructions.startswith("DEPRECATED"))
        self.assertIn("scripts/box.py", instructions)
        # The original orientation must survive — the server still works.
        self.assertIn("operator_boxes", instructions)


class BoxPointerTests(unittest.TestCase):
    """box.py routes agents to itself, not to the server it replaces."""

    def test_no_agent_facing_surface_sends_agents_to_the_mcp_server(self) -> None:
        payload = BOX.box_capabilities_payload()
        haystacks = {
            "ssh.safe_first_try": BOX._box_agent_command("ssh")["safe_first_try"],
            "exec.safe_first_try": BOX._box_agent_command("exec")["safe_first_try"],
            "safety.non_tty_alternative": payload["safety"]["non_tty_alternative"],
            "robot_docs": BOX.box_robot_docs_guide(),
        }
        for label, text in haystacks.items():
            with self.subTest(surface=label):
                self.assertNotIn("MCP operator_", text)
        self.assertIn("box.py exec", haystacks["ssh.safe_first_try"])
        self.assertIn("box.py exec", haystacks["safety.non_tty_alternative"])

    def test_capabilities_records_the_deprecation_and_the_replacement_skill(self) -> None:
        status = BOX.box_capabilities_payload()["mcp_status"]
        self.assertEqual(status["state"], "deprecated")
        self.assertEqual(status["server"], MCP.SERVER_NAME)
        self.assertEqual(status["skill"], f"{SKILL_DIR.relative_to(ROOT_DIR)}/SKILL.md")
        self.assertTrue((ROOT_DIR / status["skill"]).is_file())

    def test_capabilities_reports_full_tool_parity_and_no_gaps(self) -> None:
        status = BOX.box_capabilities_payload()["mcp_status"]
        self.assertEqual(status["gaps"], {})
        self.assertEqual(status["tool_parity"], f"{len(MCP.TOOLS)}/{len(MCP.TOOLS)}")
        self.assertEqual(set(status["non_box_replacements"]), set(NON_BOX_REPLACEMENTS))

    def test_legacy_mcp_equivalents_only_name_tools_that_exist(self) -> None:
        """Kept for translating old transcripts — it must not name phantom tools."""
        payload = BOX.box_capabilities_payload()
        tool_names = {tool["name"] for tool in MCP.TOOLS}
        for verb, tool in payload["mcp_equivalents"].items():
            with self.subTest(verb=verb):
                self.assertIn(verb, BOX.BOX_COMMAND_NAMES)
                self.assertIn(tool, tool_names)

    def test_capabilities_publishes_the_exec_command_guard(self) -> None:
        """The gate that makes CLI exec safe must be discoverable, not folklore."""
        guard = BOX.box_capabilities_payload()["command_guard"]
        self.assertEqual(guard["verbs"], ["exec"])
        self.assertEqual(guard["guard"], BOX.DCG_INTERFACE)
        self.assertEqual(guard["expected_version"], BOX.DCG_PINNED_VERSION)
        self.assertEqual(guard["advisory_sites"], list(BOX.BOX_EXEC_DCG_ADVISORY_SITES))
        self.assertEqual(sorted(guard["error_types"]), ["dcg_denied", "dcg_unavailable"])


class OperatorSkillTests(unittest.TestCase):
    """The skill an agent gets INSTEAD of the MCP server."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SKILL_FILE.read_text(encoding="utf-8")

    def test_skill_exists_with_frontmatter_matching_its_directory(self) -> None:
        lines = self.text.splitlines()
        self.assertEqual(lines[0], "---")
        end = lines.index("---", 1)
        frontmatter = "\n".join(lines[1:end])
        self.assertIn(f"name: {SKILL_DIR.name}", frontmatter)
        self.assertIn("description:", frontmatter)

    def test_skill_covers_every_mcp_tool(self) -> None:
        """No tool may be retired into a skill that never mentions it."""
        for tool in MCP.TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertIn(tool["name"], self.text)

    def test_skill_carries_the_gating_story(self) -> None:
        for phrase in (
            "--dry-run",
            "dryrun_marker_required",
            "dirty_tree_refused",
            "dcg_denied",
            "dcg_unavailable",
            "fails closed",
            "600",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)

    def test_skill_requires_user_confirmation_for_destructive_verbs(self) -> None:
        lowered = self.text.lower()
        self.assertIn("confirm with the user", lowered)
        for verb in ("down", "compose-down"):
            self.assertIn(f"box.py {verb}", self.text)

    def test_skill_does_not_teach_the_gate_escape_hatch_as_a_workaround(self) -> None:
        """The skip env var may be named, but only as a prohibition."""
        self.assertIn("SKILLBOX_CLI_MUTATION_GATE", self.text)
        self.assertIn("Never set `SKILLBOX_CLI_MUTATION_GATE=skip`", self.text)

    def test_skill_is_distinct_from_the_in_box_operator_skill(self) -> None:
        in_box = ROOT_DIR / "skills" / "skillbox-operator" / "SKILL.md"
        self.assertTrue(in_box.is_file())
        self.assertNotEqual(in_box.resolve(), SKILL_FILE.resolve())
        self.assertIn("manage.py", in_box.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
