from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
RUNTIME_MANAGER_DIR = ENV_MANAGER_DIR / "runtime_manager"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.parity_schema import (  # noqa: E402
    EnvTargetRow,
    FlagRow,
    HelperRow,
    PARITY_LEDGER_INVALID,
    ParityLedgerSchemaError,
    ServiceSurfaceRow,
    parse_ledger_row,
)


def _import_validation_module() -> types.ModuleType:
    if str(ENV_MANAGER_DIR) not in sys.path:
        sys.path.insert(0, str(ENV_MANAGER_DIR))
    if "runtime_manager" not in sys.modules:
        package = types.ModuleType("runtime_manager")
        package.__path__ = [str(RUNTIME_MANAGER_DIR)]  # type: ignore[attr-defined]
        sys.modules["runtime_manager"] = package
    return importlib.import_module("runtime_manager.validation")


def _base_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "api",
        "surface_type": "service",
        "action": "build",
        "ownership_state": "covered",
        "intended_profiles": ["local-core"],
        "bridge_dependency": None,
        "request_error": "",
        "legacy_surface": "api",
        "notes": "ready",
        "profiles": [],
        "client": "personal",
    }
    row.update(overrides)
    return row


class ParitySchemaParseTests(unittest.TestCase):
    def test_dispatches_by_surface_type(self) -> None:
        cases = [
            ("service", ServiceSurfaceRow),
            ("env_target", EnvTargetRow),
            ("helper", HelperRow),
            ("flag", FlagRow),
        ]
        for surface_type, expected_type in cases:
            with self.subTest(surface_type=surface_type):
                row = parse_ledger_row(
                    _base_row(id=f"{surface_type}-row", surface_type=surface_type),
                    index=3,
                    source="overlay.yaml",
                )
                self.assertIsInstance(row, expected_type)
                self.assertEqual(row.surface_type, surface_type)
                self.assertEqual(row.provenance["row_index"], 3)
                self.assertEqual(row.provenance["source_file"], "overlay.yaml")

    def test_legacy_bridge_surface_type_maps_to_helper_row(self) -> None:
        row = parse_ledger_row(_base_row(surface_type="bridge"))

        self.assertIsInstance(row, HelperRow)
        self.assertEqual(row.surface_type, "helper")

    def test_unknown_surface_type_fails_with_provenance(self) -> None:
        with self.assertRaises(ParityLedgerSchemaError) as raised:
            parse_ledger_row(_base_row(id="repo-row", surface_type="repo"), index=7)

        self.assertEqual(raised.exception.code, PARITY_LEDGER_INVALID)
        self.assertEqual(raised.exception.context["provenance"]["row_index"], 7)
        self.assertEqual(raised.exception.context["provenance"]["id"], "repo-row")
        self.assertIn("surface_type", raised.exception.context["issues"][0])

    def test_bad_field_types_fail_precisely(self) -> None:
        with self.assertRaises(ParityLedgerSchemaError) as raised:
            parse_ledger_row(
                _base_row(
                    id=123,
                    intended_profiles="local-core",
                    bridge_dependency=["bridge"],
                )
            )

        issues = raised.exception.context["issues"]
        self.assertIn("id must be a string", issues)
        self.assertIn("intended_profiles must be a list of strings", issues)
        self.assertIn("bridge_dependency must be a string or null", issues)

    def test_unknown_action_and_state_fail(self) -> None:
        with self.assertRaises(ParityLedgerSchemaError) as raised:
            parse_ledger_row(_base_row(action="ship", ownership_state="owned"))

        issues = raised.exception.context["issues"]
        self.assertTrue(any(issue.startswith("action must be one of:") for issue in issues))
        self.assertTrue(any(issue.startswith("ownership_state must be one of:") for issue in issues))

    def test_unknown_request_error_fails(self) -> None:
        with self.assertRaises(ParityLedgerSchemaError) as raised:
            parse_ledger_row(_base_row(ownership_state="deferred", request_error="NOT_LOCAL"))

        self.assertIn(
            "request_error must be a known LOCAL_RUNTIME_* code",
            raised.exception.context["issues"],
        )

    def test_bridge_only_missing_bridge_dependency_warns_by_default(self) -> None:
        row = parse_ledger_row(
            _base_row(
                ownership_state="bridge-only",
                bridge_dependency=None,
                request_error="LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
            )
        )

        self.assertEqual([warning.field for warning in row.warnings], ["bridge_dependency"])

    def test_bridge_only_missing_bridge_dependency_fails_when_strict(self) -> None:
        with self.assertRaises(ParityLedgerSchemaError) as raised:
            parse_ledger_row(
                _base_row(
                    ownership_state="bridge-only",
                    bridge_dependency=None,
                    request_error="LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
                ),
                strict_cross_fields=True,
            )

        self.assertIn("bridge_dependency", raised.exception.context["issues"][0])

    def test_deferred_missing_request_error_warns_by_default(self) -> None:
        row = parse_ledger_row(
            _base_row(
                ownership_state="deferred",
                request_error="",
            )
        )

        self.assertEqual([warning.field for warning in row.warnings], ["request_error"])

    def test_deferred_missing_request_error_fails_when_strict(self) -> None:
        with self.assertRaises(ParityLedgerSchemaError) as raised:
            parse_ledger_row(
                _base_row(
                    ownership_state="deferred",
                    request_error="",
                ),
                strict_cross_fields=True,
            )

        self.assertIn("request_error", raised.exception.context["issues"][0])

    def test_covered_missing_intended_profiles_warns_by_default(self) -> None:
        row = parse_ledger_row(_base_row(intended_profiles=[]))

        self.assertEqual([warning.field for warning in row.warnings], ["intended_profiles"])

    def test_covered_missing_intended_profiles_fails_when_strict(self) -> None:
        with self.assertRaises(ParityLedgerSchemaError) as raised:
            parse_ledger_row(_base_row(intended_profiles=[]), strict_cross_fields=True)

        self.assertIn("intended_profiles", raised.exception.context["issues"][0])

    def test_round_trip_returns_normalized_public_shape(self) -> None:
        parsed = parse_ledger_row(
            _base_row(
                id=" flag ",
                surface_type="flag",
                intended_profiles=[" local-core ", "", "prod"],
                profiles=[" local-all "],
                bridge_dependency="",
            )
        )

        self.assertEqual(
            parsed.to_dict(),
            {
                "id": "flag",
                "legacy_surface": "api",
                "surface_type": "flag",
                "action": "build",
                "ownership_state": "covered",
                "intended_profiles": ["local-core", "prod"],
                "bridge_dependency": None,
                "request_error": "",
                "notes": "ready",
                "profiles": ["local-all"],
                "client": "personal",
            },
        )

    def test_runtime_manager_build_wrapper_raises_typed_validation_error(self) -> None:
        validation = _import_validation_module()
        model = {
            "manifest_file": "workspace/runtime.yaml",
            "clients": [],
            "parity_ledger": [_base_row(id="bad", surface_type="repo")],
        }

        with (
            mock.patch.object(
                validation,
                "_build_runtime_model_without_parity_schema",
                return_value=model,
            ),
            self.assertRaises(validation.ValidationError) as raised,
        ):
            validation.build_runtime_model(Path("."))

        self.assertEqual(raised.exception.code, PARITY_LEDGER_INVALID)
        self.assertEqual(
            raised.exception.context["provenance"]["source_file"],
            "workspace/runtime.yaml",
        )
        self.assertEqual(raised.exception.context["provenance"]["id"], "bad")


if __name__ == "__main__":
    unittest.main()
