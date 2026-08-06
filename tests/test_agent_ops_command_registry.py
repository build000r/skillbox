from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import command_registry as REG  # noqa: E402


def _valid_spec(**changes: object) -> REG.CommandSpec:
    base = REG.CommandSpec(
        id="brain.example",
        tier=1,
        surface=("cli", "mcp"),
        summary="Example brain command for tests.",
        inputs={"client": "string?", "format": "enum[json|text]?"},
        outputs={"ok": "boolean", "results": "object[]"},
        side_effect="none",
        risk="low",
        entrypoint="manage.py",
        mcp_tool="skillbox_example",
        scopes=("client",),
        examples=("python3 .env-manager/manage.py example --format json",),
        validations=("python3 -m unittest tests.test_agent_ops_command_registry",),
        graph_nodes=("command",),
    )
    return REG.replace_spec(base, **changes) if changes else base


class DefaultRegistryTests(unittest.TestCase):
    def test_default_registry_validates_clean(self) -> None:
        issues = REG.validate_registry(REG.default_registry())
        self.assertEqual(issues, [])

    def test_load_default_registry_returns_specs_by_id(self) -> None:
        registry = REG.load_default_registry()
        self.assertIn("brain.next", registry)
        self.assertIsInstance(registry["brain.next"], REG.CommandSpec)
        self.assertEqual(len(registry), len(REG.default_registry()))

    def test_required_tier1_brain_commands_have_full_abi(self) -> None:
        registry = REG.load_default_registry()
        for required in sorted(REG.REQUIRED_TIER1_IDS):
            spec = registry.get(required)
            self.assertIsNotNone(spec, f"missing tier 1 entry {required}")
            self.assertEqual(spec.tier, 1)
            self.assertTrue(spec.inputs, f"{required} must declare inputs")
            self.assertTrue(spec.outputs, f"{required} must declare outputs")
            self.assertTrue(spec.examples, f"{required} must declare examples")
            self.assertTrue(spec.validations, f"{required} must declare validations")
            self.assertTrue(spec.graph_nodes, f"{required} must declare graph_nodes")
            if "mcp" in spec.surface:
                self.assertTrue(spec.mcp_tool, f"{required} must declare mcp_tool")

    def test_required_tier2_surfaces_are_covered(self) -> None:
        registry = REG.load_default_registry()
        for required in sorted(REG.REQUIRED_TIER2_IDS):
            self.assertIn(required, registry, f"missing tier 2 entry {required}")

    def test_operator_lifecycle_mcp_tools_are_not_in_box_brain_tools(self) -> None:
        registry = REG.load_default_registry()
        entrypoints = {spec.entrypoint for spec in registry.values()}
        tools = {spec.mcp_tool for spec in registry.values() if spec.mcp_tool}
        self.assertNotIn("operator_mcp_server.py", entrypoints)
        self.assertFalse(any(tool.startswith("operator_") for tool in tools))

    def test_brain_mcp_tools_mirror_shared_contract(self) -> None:
        registry = REG.load_default_registry()
        declared = {spec.mcp_tool for spec in registry.values() if spec.mcp_tool}
        for tool in (
            "skillbox_capabilities",
            "skillbox_next",
            "skillbox_graph",
            "skillbox_explain",
            "skillbox_search",
            "skillbox_snap",
        ):
            self.assertIn(tool, declared)

    def test_destructive_entries_carry_destructive_risk(self) -> None:
        for spec in REG.default_registry():
            if spec.side_effect == "destructive":
                self.assertEqual(spec.risk, "destructive", spec.id)

    def test_read_only_brain_surfaces_declare_no_side_effects(self) -> None:
        registry = REG.load_default_registry()
        for command_id in ("brain.next", "brain.graph", "brain.explain", "brain.search"):
            self.assertEqual(registry[command_id].side_effect, "none", command_id)

    def test_clipboard_cli_surfaces_have_distinct_real_output_contracts(self) -> None:
        registry = REG.load_default_registry()
        status = registry["clipboard.status"]
        doctor = registry["clipboard.doctor"]
        explain = registry["clipboard.explain"]
        self.assertEqual(status.entrypoint, "clipboard-paste")
        self.assertEqual(doctor.outputs, status.outputs)
        self.assertIn("target_probe", status.outputs)
        self.assertNotIn("target_probe", explain.outputs)
        self.assertIn("image_action", explain.outputs)

    def test_missing_required_entry_is_reported(self) -> None:
        specs = [s for s in REG.default_registry() if s.id != "brain.next"]
        issues = REG.validate_registry(specs)
        self.assertTrue(any("missing required tier 1 entry 'brain.next'" in i for i in issues))


class SerializationTests(unittest.TestCase):
    def test_payload_round_trip_for_every_default_spec(self) -> None:
        for spec in REG.default_registry():
            rebuilt = REG.spec_from_payload(spec.to_payload())
            self.assertEqual(rebuilt, spec, spec.id)

    def test_payload_omits_unset_optional_fields(self) -> None:
        spec = _valid_spec(
            tier=2,
            surface=("cli",),
            mcp_tool=None,
            owner_binary=None,
            scopes=(),
            validations=(),
            graph_nodes=(),
        )
        payload = spec.to_payload()
        for absent in ("owner_binary", "mcp_tool", "scopes", "validations", "graph_nodes"):
            self.assertNotIn(absent, payload)

    def test_payload_key_order_is_stable(self) -> None:
        spec = _valid_spec(owner_binary="sbp")
        self.assertEqual(
            list(spec.to_payload()),
            [
                "id",
                "tier",
                "surface",
                "summary",
                "inputs",
                "outputs",
                "side_effect",
                "risk",
                "entrypoint",
                "owner_binary",
                "mcp_tool",
                "scopes",
                "examples",
                "validations",
                "graph_nodes",
            ],
        )

    def test_spec_from_payload_defaults_missing_optionals(self) -> None:
        spec = REG.spec_from_payload(
            {
                "id": "runtime.minimal",
                "tier": 2,
                "surface": ["cli"],
                "summary": "Minimal entry.",
                "inputs": {},
                "outputs": {"ok": "boolean"},
                "side_effect": "none",
                "risk": "low",
                "entrypoint": "manage.py",
                "examples": ["manage.py minimal"],
            }
        )
        self.assertIsNone(spec.owner_binary)
        self.assertIsNone(spec.mcp_tool)
        self.assertEqual(spec.scopes, ())
        self.assertEqual(spec.validations, ())
        self.assertEqual(spec.graph_nodes, ())

    def test_spec_from_payload_ignores_unknown_keys_and_string_surface(self) -> None:
        spec = REG.spec_from_payload(
            {
                "id": "runtime.forward",
                "tier": 2,
                "surface": "cli",
                "summary": "Forward-compatible entry.",
                "inputs": {},
                "outputs": {"ok": "boolean"},
                "side_effect": "none",
                "risk": "low",
                "entrypoint": "manage.py",
                "examples": ["manage.py forward"],
                "future_field": {"ignored": True},
            }
        )
        self.assertEqual(spec.surface, ("cli",))
        self.assertEqual(REG.validate_spec(spec), [])

    def test_registry_payload_shape_and_json_round_trip(self) -> None:
        payload = REG.registry_payload()
        self.assertEqual(payload["abi_version"], REG.REGISTRY_ABI_VERSION)
        ids = [entry["id"] for entry in payload["capabilities"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(payload["counts"]["total"], len(ids))
        self.assertEqual(
            payload["counts"]["total"],
            payload["counts"]["tier1"] + payload["counts"]["tier2"],
        )
        decoded = json.loads(json.dumps(payload))
        self.assertEqual(decoded, payload)

    def test_registry_text_lines_are_compact(self) -> None:
        lines = REG.registry_text_lines()
        specs = REG.default_registry()
        self.assertEqual(len(lines), len(specs) + 1)
        self.assertIn("command registry", lines[0])
        self.assertIn(f"{len(specs)} entries", lines[0])
        for line in lines[1:]:
            self.assertLessEqual(len(line), 200)
            self.assertIn("risk=", line)
            self.assertIn("effect=", line)


class ValidationTests(unittest.TestCase):
    def test_valid_spec_has_no_issues(self) -> None:
        self.assertEqual(REG.validate_spec(_valid_spec()), [])

    def test_invalid_id_is_reported(self) -> None:
        issues = REG.validate_spec(_valid_spec(id="Bad Id!"))
        self.assertTrue(any("id must match" in i for i in issues))

    def test_unknown_enum_values_are_reported(self) -> None:
        cases = {
            "surface": _valid_spec(surface=("gui",)),
            "side_effect": _valid_spec(side_effect="writes_stuff"),
            "risk": _valid_spec(risk="extreme"),
            "scope": _valid_spec(scopes=("tenant",)),
            "graph node": _valid_spec(graph_nodes=("cluster",)),
            "entrypoint": _valid_spec(entrypoint="mystery.py"),
            "owner_binary": _valid_spec(owner_binary="sbx"),
        }
        for label, spec in cases.items():
            issues = REG.validate_spec(spec)
            self.assertTrue(issues, f"expected issues for unknown {label}")

    def test_empty_surface_is_reported(self) -> None:
        issues = REG.validate_spec(_valid_spec(surface=()))
        self.assertTrue(any("surface must declare" in i for i in issues))

    def test_multiline_summary_is_reported(self) -> None:
        issues = REG.validate_spec(_valid_spec(summary="line one\nline two"))
        self.assertTrue(any("single line" in i for i in issues))

    def test_destructive_side_effect_requires_destructive_risk(self) -> None:
        issues = REG.validate_spec(_valid_spec(side_effect="destructive", risk="low"))
        self.assertTrue(any("destructive side_effect requires destructive risk" in i for i in issues))

    def test_destructive_risk_requires_destructive_side_effect(self) -> None:
        issues = REG.validate_spec(_valid_spec(side_effect="none", risk="destructive"))
        self.assertTrue(any("destructive risk requires destructive side_effect" in i for i in issues))

    def test_no_side_effect_cannot_be_high_risk(self) -> None:
        issues = REG.validate_spec(_valid_spec(side_effect="none", risk="high"))
        self.assertTrue(any("cannot carry" in i for i in issues))

    def test_missing_examples_are_reported(self) -> None:
        issues = REG.validate_spec(_valid_spec(examples=()))
        self.assertTrue(any("examples" in i for i in issues))
        issues = REG.validate_spec(_valid_spec(examples=("  ",)))
        self.assertTrue(any("examples" in i for i in issues))

    def test_invalid_input_type_declarations_are_reported(self) -> None:
        issues = REG.validate_spec(_valid_spec(inputs={"client": "str"}))
        self.assertTrue(any("invalid type declaration" in i for i in issues))
        issues = REG.validate_spec(_valid_spec(outputs={"ok": 7}))
        self.assertTrue(any("type string or nested mapping" in i for i in issues))

    def test_nested_schema_mappings_are_allowed(self) -> None:
        spec = _valid_spec(outputs={"ok": "boolean", "context": {"client": "string?", "profile": "string[]?"}})
        self.assertEqual(REG.validate_spec(spec), [])

    def test_tier1_requires_full_abi(self) -> None:
        issues = REG.validate_spec(
            _valid_spec(inputs={}, outputs={}, validations=(), graph_nodes=(), mcp_tool=None)
        )
        joined = "\n".join(issues)
        self.assertIn("tier 1 entries must declare an inputs schema", joined)
        self.assertIn("tier 1 entries must declare an outputs schema", joined)
        self.assertIn("tier 1 entries must declare validations", joined)
        self.assertIn("tier 1 entries must declare graph_nodes", joined)
        self.assertIn("tier 1 mcp surfaces must declare mcp_tool", joined)

    def test_tier2_allows_missing_optionals(self) -> None:
        spec = _valid_spec(
            tier=2,
            surface=("cli",),
            mcp_tool=None,
            scopes=(),
            validations=(),
            graph_nodes=(),
        )
        self.assertEqual(REG.validate_spec(spec), [])

    def test_mcp_tool_without_mcp_surface_is_reported(self) -> None:
        issues = REG.validate_spec(_valid_spec(surface=("cli",)))
        self.assertTrue(any("surface does not include mcp" in i for i in issues))

    def test_invalid_tier_is_reported(self) -> None:
        issues = REG.validate_spec(_valid_spec(tier=3))
        self.assertTrue(any("tier must be one of" in i for i in issues))

    def test_duplicate_ids_are_reported(self) -> None:
        specs = list(REG.default_registry())
        specs.append(specs[0])
        issues = REG.validate_registry(specs)
        self.assertTrue(any("duplicate command id" in i for i in issues))

    def test_duplicate_mcp_tools_are_reported(self) -> None:
        first = _valid_spec(id="brain.first", mcp_tool="skillbox_shared")
        second = _valid_spec(id="brain.second", mcp_tool="skillbox_shared")
        issues = REG.validate_registry(
            list(REG.default_registry()) + [first, second]
        )
        self.assertTrue(any("already declared by" in i for i in issues))

    def test_registry_validation_error_carries_issues(self) -> None:
        broken = [_valid_spec(risk="extreme")]
        issues = REG.validate_registry(broken)
        error = REG.RegistryValidationError(issues)
        self.assertEqual(error.issues, issues)
        self.assertIn("command registry validation failed", str(error))


if __name__ == "__main__":
    unittest.main()
