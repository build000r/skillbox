"""Tests for the typed error hierarchy + one JSON envelope (errors.py).

Covers:
  * one unit test per SkillboxError subclass,
  * the envelope-shape contract (to_payload renders ok/error/error.code/...),
  * back-compat: legacy keys (error, error_code, error.type, error.recoverable)
    COEXIST with the new keys (error.code, error.context, error.next_actions),
  * the deprecation marker,
  * a KNOWN-CODES snapshot proving codes are UNCHANGED by this carrier change,
  * 100% branch coverage of errors.py (internal_error_payload both branches,
    empty vs non-empty context/next_actions, __str__).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import errors as ERR  # noqa: E402
from runtime_manager.errors import (  # noqa: E402
    AdapterError,
    NetworkError,
    RuntimeLifecycleError,
    SkillboxError,
    StateConflictError,
    ValidationError,
    internal_error_payload,
)


SUBCLASSES = (
    ValidationError,
    RuntimeLifecycleError,
    StateConflictError,
    AdapterError,
    NetworkError,
)


class SubclassTests(unittest.TestCase):
    def test_exactly_five_subclasses_and_all_extend_runtimeerror(self) -> None:
        # The hierarchy is DELIBERATELY shallow: exactly five subclasses, and
        # every one inherits from SkillboxError -> RuntimeError so existing
        # `except RuntimeError` sites keep catching them for one release.
        self.assertEqual(len(SUBCLASSES), 5)
        for cls in SUBCLASSES:
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, SkillboxError))
                self.assertTrue(issubclass(cls, RuntimeError))

    def test_each_subclass_carries_code_message_context_next_actions(self) -> None:
        for cls in SUBCLASSES:
            with self.subTest(cls=cls.__name__):
                exc = cls(
                    "runtime_error",
                    f"{cls.__name__} failed",
                    context={"k": "v"},
                    next_actions=["doctor --format json"],
                    recoverable=False,
                )
                self.assertEqual(exc.code, "runtime_error")
                self.assertEqual(exc.message, f"{cls.__name__} failed")
                self.assertEqual(exc.context, {"k": "v"})
                self.assertEqual(exc.next_actions, ["doctor --format json"])
                self.assertFalse(exc.recoverable)
                # __str__ returns the message (load-bearing for text-mode print).
                self.assertEqual(str(exc), f"{cls.__name__} failed")

    def test_can_be_raised_and_caught_as_runtimeerror(self) -> None:
        for cls in SUBCLASSES:
            with self.subTest(cls=cls.__name__):
                with self.assertRaises(RuntimeError):
                    raise cls("runtime_error", "boom")


class EnvelopeContractTests(unittest.TestCase):
    def test_to_payload_full_envelope_shape(self) -> None:
        exc = ValidationError(
            "unknown_client",
            "Unknown runtime client(s): x.",
            context={"unknown": ["x"]},
            next_actions=["render --format json"],
        )
        payload = exc.to_payload()
        # New canonical envelope.
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error"]["code"], "unknown_client")
        self.assertEqual(payload["error"]["message"], "Unknown runtime client(s): x.")
        self.assertEqual(payload["error"]["context"], {"unknown": ["x"]})
        self.assertEqual(payload["error"]["next_actions"], ["render --format json"])
        # Legacy mirrors COEXIST.
        self.assertEqual(payload["error"]["type"], "unknown_client")
        self.assertTrue(payload["error"]["recoverable"])
        self.assertEqual(payload["error_code"], "unknown_client")
        self.assertEqual(payload["next_actions"], ["render --format json"])

    def test_code_type_and_error_code_always_agree(self) -> None:
        exc = AdapterError("runtime_error", "git clone failed")
        payload = exc.to_payload()
        self.assertEqual(payload["error"]["code"], payload["error"]["type"])
        self.assertEqual(payload["error"]["code"], payload["error_code"])

    def test_empty_context_and_next_actions_are_omitted(self) -> None:
        # Branch coverage: no context, no next_actions -> keys absent, and no
        # top-level next_actions added.
        payload = NetworkError("runtime_error", "digest mismatch").to_payload()
        self.assertNotIn("context", payload["error"])
        self.assertNotIn("next_actions", payload["error"])
        self.assertNotIn("next_actions", payload)
        # ...but ok/error_code/deprecation are always present.
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error_code"], "runtime_error")
        self.assertIn("deprecation", payload)

    def test_to_payload_returns_copies_not_shared_references(self) -> None:
        ctx = {"a": 1}
        actions = ["doctor --format json"]
        exc = StateConflictError("runtime_error", "cycle", context=ctx, next_actions=actions)
        payload = exc.to_payload()
        payload["error"]["context"]["a"] = 999
        payload["error"]["next_actions"].append("mutated")
        payload["deprecation"]["legacy_keys"].append("mutated")
        # Mutating the payload must not corrupt the source error or the marker.
        self.assertEqual(exc.context, {"a": 1})
        self.assertEqual(exc.next_actions, ["doctor --format json"])
        self.assertNotIn("mutated", ERR.DEPRECATION_MARKER["legacy_keys"])


class DeprecationMarkerTests(unittest.TestCase):
    def test_marker_lists_legacy_and_new_keys(self) -> None:
        marker = ValidationError("runtime_error", "x").to_payload()["deprecation"]
        self.assertIn("error_code", marker["legacy_keys"])
        self.assertIn("error.type", marker["legacy_keys"])
        self.assertIn("error.code", marker["use_instead"])
        self.assertIn("error.context", marker["use_instead"])
        self.assertTrue(marker["note"])


class InternalErrorTests(unittest.TestCase):
    def test_internal_error_payload_without_context(self) -> None:
        payload = internal_error_payload("Unexpected error: boom")
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error"]["code"], ERR.INTERNAL_ERROR_CODE)
        self.assertEqual(payload["error_code"], "INTERNAL")
        self.assertFalse(payload["error"]["recoverable"])
        self.assertNotIn("context", payload["error"])

    def test_internal_error_payload_with_context_and_next_actions(self) -> None:
        payload = internal_error_payload(
            "Unexpected error: boom",
            context={"traceback": "Traceback (most recent call last): ..."},
            next_actions=["doctor --format json"],
        )
        self.assertIn("traceback", payload["error"]["context"])
        self.assertEqual(payload["next_actions"], ["doctor --format json"])
        self.assertEqual(payload["error"]["next_actions"], ["doctor --format json"])


# ---------------------------------------------------------------------------
# KNOWN-CODES SNAPSHOT
# ---------------------------------------------------------------------------
#
# This is a CARRIER change, not a renumbering. The codes below are the EXACT
# string codes that existed before the typed hierarchy: the message-pattern
# error_types from classify_error, the LOCAL_RUNTIME_* contract codes, the
# PERSISTENCE_* / state-root codes, plus the generic INTERNAL fallback. If this
# set changes, a code was renamed/added/removed — review deliberately.

KNOWN_CLASSIFY_CODES = frozenset({
    "runtime_error",
    "blueprint_not_found",
    "client_overlay_missing",
    "conflict",
    "invalid_client_id",
    "invalid_scaffold_pack",
    "invalid_target_repo",
    "missing_argument",
    "missing_env_file",
    "missing_target_repo",
    "missing_variable",
    "service_health_failure",
    "session_not_found",
    "session_state_conflict",
    "unknown_client",
})

KNOWN_LOCAL_RUNTIME_CODES = frozenset({
    "LOCAL_RUNTIME_ENV_BRIDGE_FAILED",
    "LOCAL_RUNTIME_ENV_OUTPUT_MISSING",
    "LOCAL_RUNTIME_PROFILE_UNKNOWN",
    "LOCAL_RUNTIME_START_BLOCKED",
    "LOCAL_RUNTIME_SERVICE_DEFERRED",
    "LOCAL_RUNTIME_MODE_UNSUPPORTED",
    "LOCAL_RUNTIME_COVERAGE_GAP",
})

KNOWN_PERSISTENCE_CODES = frozenset({
    "PERSISTENCE_CONFIG_INVALID",
    "PERSISTENCE_CLASS_UNKNOWN",
    "PERSISTENCE_BINDING_CONFLICT",
    "DO_VOLUME_LAYOUT_MISSING",
    "STATE_ROOT_MISSING",
    "STATE_ROOT_WRONG_FILESYSTEM",
    "STATE_ROOT_WRONG_OWNERSHIP",
    "STATE_ROOT_LOW_SPACE",
    "PERSISTENT_PATH_OFF_STATE_ROOT",
})

KNOWN_INTERNAL_CODES = frozenset({"INTERNAL"})

KNOWN_ERROR_CODES = (
    KNOWN_CLASSIFY_CODES
    | KNOWN_LOCAL_RUNTIME_CODES
    | KNOWN_PERSISTENCE_CODES
    | KNOWN_INTERNAL_CODES
)


class KnownCodesSnapshotTests(unittest.TestCase):
    def test_local_runtime_codes_match_runtime_model_constants(self) -> None:
        from lib import runtime_model

        self.assertEqual(
            set(runtime_model.LOCAL_RUNTIME_ERROR_CODES),
            set(KNOWN_LOCAL_RUNTIME_CODES),
        )

    def test_persistence_codes_match_runtime_model_constants(self) -> None:
        from lib import runtime_model

        self.assertEqual(
            set(runtime_model.PERSISTENCE_ERROR_CODES),
            set(KNOWN_PERSISTENCE_CODES),
        )

    def test_classify_message_pattern_codes_unchanged(self) -> None:
        # Re-derive the message-pattern error_types from the live source and
        # pin them to the snapshot, so a renamed code is caught.
        import inspect
        import re

        from runtime_manager import shared

        src = inspect.getsource(shared._classify_message_pattern)  # noqa: SLF001
        derived = set(re.findall(r'error_type="([^"]+)"', src))
        derived.add("runtime_error")  # the classify_error default fallback
        self.assertEqual(derived, set(KNOWN_CLASSIFY_CODES))

    def test_internal_fallback_code_is_stable(self) -> None:
        self.assertEqual(ERR.INTERNAL_ERROR_CODE, "INTERNAL")
        self.assertIn("INTERNAL", KNOWN_ERROR_CODES)

    def test_full_known_codes_snapshot_is_frozen(self) -> None:
        # The single enumeration the acceptance criterion references. Adding or
        # renaming a code requires editing this snapshot ON PURPOSE.
        self.assertEqual(len(KNOWN_ERROR_CODES), 32)


if __name__ == "__main__":
    unittest.main()
