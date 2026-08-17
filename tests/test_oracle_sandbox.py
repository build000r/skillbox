"""Chrome sandbox posture for the Oracle host, and its no-false-green contract.

The cookie-bearing Chrome runs with ``--no-sandbox``. The single property this
suite exists to defend is that no arrangement of waivers, controls, or evidence
can make that read as healthy — asserted exhaustively over the whole input
space, not sampled — while a fully compensated, unexpired exception still
reports as an accepted posture rather than a permanent red an operator learns
to skip.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import structure_doctor as SD  # noqa: E402
from runtime_manager.oracle_broker import OracleBrokerError  # noqa: E402
from runtime_manager.oracle_sandbox import (  # noqa: E402
    MAX_WAIVER_DURATION_MS,
    ORACLE_SANDBOX_DECLARATION_SCHEMA,
    REFUSAL_CODES,
    REQUIRED_CONTROLS,
    STATE_ENFORCED,
    STATE_UNCONTAINED,
    STATE_UNDECLARED,
    STATE_WAIVED,
    VERDICT_FAIL,
    VERDICT_INCONCLUSIVE,
    VERDICT_PASS,
    WAIVER_REASONS,
    ChromeSandboxEvidence,
    CompensatingControl,
    OracleSandboxError,
    SandboxWaiver,
    declaration_path,
    evaluate_sandbox_posture,
    posture_from_declaration,
    undeclared_posture,
)

SANDBOX_SOURCE = ENV_MANAGER_DIR / "runtime_manager" / "oracle_sandbox.py"

HOST = "d3"
NOW = 1_700_000_000_000
DAY_MS = 24 * 60 * 60 * 1000
EVIDENCE_TOKEN = "systemd:ProtectSystem=strict"


def controls(*, verified: bool = True, withhold: str = "") -> list[CompensatingControl]:
    return [
        CompensatingControl(
            name=name,
            verified=verified and name != withhold,
            evidence=EVIDENCE_TOKEN if (verified and name != withhold) else "",
        )
        for name in REQUIRED_CONTROLS
    ]


def controls_from_flags(flags: tuple[bool, ...]) -> list[CompensatingControl]:
    return [
        CompensatingControl(
            name=name,
            verified=flag,
            evidence=EVIDENCE_TOKEN if flag else "",
        )
        for name, flag in zip(REQUIRED_CONTROLS, flags)
    ]


def waiver(**overrides: object) -> SandboxWaiver:
    values: dict[str, object] = {
        "host": HOST,
        "reason": "userns_unavailable",
        "approved_by": "operator",
        "approved_at_ms": NOW - DAY_MS,
        "expires_at_ms": NOW + 30 * DAY_MS,
    }
    values.update(overrides)
    return SandboxWaiver(**values)  # type: ignore[arg-type]


def evidence(no_sandbox: bool, userns: bool = False, setuid: bool = False):
    return ChromeSandboxEvidence(
        no_sandbox_flag=no_sandbox,
        user_namespaces_available=userns,
        setuid_sandbox_present=setuid,
    )


def declaration_document(now_ms: int = NOW, **overrides: object) -> dict[str, object]:
    """A complete, waived declaration anchored to a caller-chosen clock.

    The doctor gate reads the wall clock, so its fixtures must be relative to
    real time; the pure evaluator's fixtures are relative to a fixed NOW.
    """
    document: dict[str, object] = {
        "schema": ORACLE_SANDBOX_DECLARATION_SCHEMA,
        "host": HOST,
        "evidence": {
            "no_sandbox_flag": True,
            "user_namespaces_available": False,
            "setuid_sandbox_present": False,
        },
        "controls": {
            name: {"verified": True, "evidence": EVIDENCE_TOKEN}
            for name in REQUIRED_CONTROLS
        },
        "waiver": {
            "host": HOST,
            "reason": "userns_unavailable",
            "approved_by": "operator",
            "approved_at_ms": now_ms - DAY_MS,
            "expires_at_ms": now_ms + 30 * DAY_MS,
        },
    }
    document.update(overrides)
    return document


class SandboxTestCase(unittest.TestCase):
    def assert_refused(self, code: str, action: object) -> OracleSandboxError:
        with self.assertRaises(OracleSandboxError) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(code, caught.exception.code)
        return caught.exception


class NoFalseGreenTests(SandboxTestCase):
    """The one property that matters, proven over the entire input space."""

    def test_no_sandbox_can_never_report_green(self) -> None:
        expired = waiver(
            approved_at_ms=NOW - 40 * DAY_MS, expires_at_ms=NOW - DAY_MS
        )
        waivers = (None, waiver(), expired, waiver(host="other-host"))
        mechanisms = ((False, False), (True, False), (False, True), (True, True))
        control_space = list(itertools.product((True, False), repeat=len(REQUIRED_CONTROLS)))
        self.assertEqual(16, len(control_space))

        checked = 0
        for flags in control_space:
            for userns, setuid in mechanisms:
                for candidate in waivers:
                    posture = evaluate_sandbox_posture(
                        evidence(True, userns, setuid),
                        controls_from_flags(flags),
                        candidate,
                        now_ms=NOW,
                        host=HOST,
                    )
                    checked += 1
                    self.assertFalse(posture.green, (flags, userns, setuid))
                    self.assertNotEqual(STATE_ENFORCED, posture.state)
                    self.assertNotEqual(VERDICT_PASS, posture.verdict)
                    self.assertIn("no_sandbox_flag", posture.reasons)
        self.assertEqual(len(control_space) * 4 * 4, checked)

    def test_only_an_enforced_sandbox_is_green(self) -> None:
        posture = evaluate_sandbox_posture(
            evidence(False, userns=True), controls(), None, now_ms=NOW, host=HOST
        )
        self.assertEqual(STATE_ENFORCED, posture.state)
        self.assertEqual(VERDICT_PASS, posture.verdict)
        self.assertTrue(posture.green)
        self.assertEqual((), posture.reasons)

    def test_the_waived_state_is_the_best_a_disabled_sandbox_can_reach(self) -> None:
        posture = evaluate_sandbox_posture(
            evidence(True), controls(), waiver(), now_ms=NOW, host=HOST
        )
        self.assertEqual(STATE_WAIVED, posture.state)
        self.assertEqual(VERDICT_INCONCLUSIVE, posture.verdict)
        self.assertFalse(posture.green)
        self.assertTrue(posture.waiver_active)
        self.assertEqual((), posture.unverified_controls)

    def test_the_waived_detail_says_disabled_out_loud(self) -> None:
        posture = evaluate_sandbox_posture(
            evidence(True), controls(), waiver(), now_ms=NOW, host=HOST
        )
        detail = posture.detail()
        self.assertIn("DISABLED", detail)
        self.assertIn("expires", detail)
        self.assertNotIn("enforced", detail)


class EnforcedPathTests(SandboxTestCase):
    """A sandbox Chrome cannot actually use is not an enforced sandbox."""

    def test_no_flag_but_no_mechanism_is_uncontained(self) -> None:
        posture = evaluate_sandbox_posture(
            evidence(False), controls(), None, now_ms=NOW, host=HOST
        )
        self.assertEqual(STATE_UNCONTAINED, posture.state)
        self.assertEqual(VERDICT_FAIL, posture.verdict)
        self.assertEqual(("sandbox_unavailable",), posture.reasons)

    def test_either_mechanism_satisfies_the_enforced_state(self) -> None:
        for userns, setuid in ((True, False), (False, True), (True, True)):
            posture = evaluate_sandbox_posture(
                evidence(False, userns, setuid),
                controls(),
                None,
                now_ms=NOW,
                host=HOST,
            )
            self.assertEqual(STATE_ENFORCED, posture.state, (userns, setuid))


class CompensatingControlTests(SandboxTestCase):
    """All four controls, or the exception is not accepted."""

    def test_withholding_any_single_control_breaks_containment(self) -> None:
        for name in REQUIRED_CONTROLS:
            posture = evaluate_sandbox_posture(
                evidence(True),
                controls(withhold=name),
                waiver(),
                now_ms=NOW,
                host=HOST,
            )
            self.assertEqual(STATE_UNCONTAINED, posture.state, name)
            self.assertIn("control_unverified", posture.reasons)
            self.assertEqual((name,), posture.unverified_controls)

    def test_a_control_cannot_claim_verified_without_evidence(self) -> None:
        # An unevidenced claim is exactly how a containment check goes quietly
        # green, so it is refused rather than believed.
        self.assert_refused(
            "control_evidence_missing",
            lambda: CompensatingControl(
                name=REQUIRED_CONTROLS[0], verified=True, evidence=""
            ),
        )

    def test_control_evidence_must_be_a_short_non_secret_token(self) -> None:
        for bad in ("x" * 200, "has space", "/home/b/.config/secret token"):
            self.assert_refused(
                "control_evidence_missing",
                lambda bad=bad: CompensatingControl(
                    name=REQUIRED_CONTROLS[0], verified=True, evidence=bad
                ),
            )

    def test_an_unknown_control_name_is_refused(self) -> None:
        self.assert_refused(
            "sandbox_input_invalid",
            lambda: CompensatingControl(name="vibes", verified=True, evidence="x"),
        )

    def test_every_control_must_be_reported(self) -> None:
        # A missing entry is indistinguishable from an unverified one, so a
        # partial report must not be accepted as a full one.
        partial = controls()[:-1]
        self.assert_refused(
            "sandbox_input_invalid",
            lambda: evaluate_sandbox_posture(
                evidence(True), partial, waiver(), now_ms=NOW, host=HOST
            ),
        )


class WaiverTests(SandboxTestCase):
    """An exception is dated, host-scoped, and approved, or it is not one."""

    def test_a_missing_waiver_is_uncontained(self) -> None:
        posture = evaluate_sandbox_posture(
            evidence(True), controls(), None, now_ms=NOW, host=HOST
        )
        self.assertEqual(STATE_UNCONTAINED, posture.state)
        self.assertIn("waiver_absent", posture.reasons)

    def test_an_expired_waiver_is_uncontained(self) -> None:
        stale = waiver(approved_at_ms=NOW - 40 * DAY_MS, expires_at_ms=NOW - DAY_MS)
        posture = evaluate_sandbox_posture(
            evidence(True), controls(), stale, now_ms=NOW, host=HOST
        )
        self.assertEqual(STATE_UNCONTAINED, posture.state)
        self.assertIn("waiver_expired", posture.reasons)
        self.assertFalse(posture.waiver_active)

    def test_a_waiver_expires_exactly_at_its_deadline(self) -> None:
        deadline = waiver(expires_at_ms=NOW)
        posture = evaluate_sandbox_posture(
            evidence(True), controls(), deadline, now_ms=NOW, host=HOST
        )
        self.assertEqual(STATE_UNCONTAINED, posture.state)
        self.assertIn("waiver_expired", posture.reasons)

    def test_a_waiver_does_not_travel_between_hosts(self) -> None:
        posture = evaluate_sandbox_posture(
            evidence(True), controls(), waiver(host="other-host"), now_ms=NOW, host=HOST
        )
        self.assertEqual(STATE_UNCONTAINED, posture.state)
        self.assertIn("waiver_host_mismatch", posture.reasons)

    def test_an_unbounded_waiver_is_refused(self) -> None:
        # An exception with no practical end date is a policy change wearing a
        # waiver's clothes.
        self.assert_refused(
            "waiver_invalid",
            lambda: waiver(expires_at_ms=NOW - DAY_MS + MAX_WAIVER_DURATION_MS + DAY_MS * 2),
        )

    def test_waiver_fields_are_validated(self) -> None:
        for override in (
            {"host": "NOT VALID"},
            {"host": ""},
            {"reason": "because"},
            {"approved_by": "a b"},
            {"approved_at_ms": -1},
            {"expires_at_ms": True},
            {"expires_at_ms": NOW - 40 * DAY_MS},
        ):
            self.assert_refused(
                "waiver_invalid", lambda override=override: waiver(**override)
            )

    def test_the_reason_vocabulary_is_closed(self) -> None:
        for reason in sorted(WAIVER_REASONS):
            self.assertTrue(waiver(reason=reason).active_at(NOW))


class DeclarationFileTests(SandboxTestCase):
    """A declaration that exists and is wrong is a finding, never a shrug."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_root = Path(temporary.name).resolve() / "state"
        (self.state_root / "oracle").mkdir(parents=True)
        os.chmod(self.state_root / "oracle", 0o700)

    def write(self, document: object) -> Path:
        path = declaration_path(self.state_root)
        path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def declaration(self, **overrides: object) -> dict[str, object]:
        return declaration_document(NOW, **overrides)

    def test_an_absent_declaration_is_undeclared_not_a_failure(self) -> None:
        posture = posture_from_declaration(self.state_root, now_ms=NOW)
        self.assertEqual(STATE_UNDECLARED, posture.state)
        self.assertEqual(VERDICT_INCONCLUSIVE, posture.verdict)
        self.assertFalse(posture.green)
        self.assertEqual(undeclared_posture().state, posture.state)

    def test_a_complete_waived_declaration_round_trips(self) -> None:
        self.write(self.declaration())
        posture = posture_from_declaration(self.state_root, now_ms=NOW)
        self.assertEqual(STATE_WAIVED, posture.state)
        self.assertTrue(posture.waiver_active)

    def test_a_declaration_without_a_waiver_is_uncontained(self) -> None:
        document = self.declaration()
        del document["waiver"]
        self.write(document)
        posture = posture_from_declaration(self.state_root, now_ms=NOW)
        self.assertEqual(STATE_UNCONTAINED, posture.state)

    def test_an_enforced_declaration_passes(self) -> None:
        document = self.declaration(
            evidence={
                "no_sandbox_flag": False,
                "user_namespaces_available": True,
                "setuid_sandbox_present": False,
            }
        )
        self.write(document)
        posture = posture_from_declaration(self.state_root, now_ms=NOW)
        self.assertEqual(STATE_ENFORCED, posture.state)
        self.assertTrue(posture.green)

    def test_malformed_declarations_refuse(self) -> None:
        cases = (
            b"{not json",
            b"[]",
            json.dumps({"schema": "other.v1"}).encode(),
            json.dumps(self.declaration(host="NOT VALID")).encode(),
        )
        path = declaration_path(self.state_root)
        for raw in cases:
            path.write_bytes(raw)
            os.chmod(path, 0o600)
            with self.assertRaises(OracleSandboxError):
                posture_from_declaration(self.state_root, now_ms=NOW)

    def test_missing_and_unknown_keys_refuse(self) -> None:
        document = self.declaration()
        del document["controls"]
        self.write(document)
        self.assert_refused(
            "declaration_invalid",
            lambda: posture_from_declaration(self.state_root, now_ms=NOW),
        )
        self.write(self.declaration(extra="x"))
        self.assert_refused(
            "declaration_invalid",
            lambda: posture_from_declaration(self.state_root, now_ms=NOW),
        )

    def test_a_group_readable_declaration_refuses(self) -> None:
        path = self.write(self.declaration())
        os.chmod(path, 0o644)
        self.assert_refused(
            "declaration_permissions",
            lambda: posture_from_declaration(self.state_root, now_ms=NOW),
        )

    def test_a_symlinked_declaration_refuses(self) -> None:
        path = self.write(self.declaration())
        elsewhere = path.parent / "elsewhere.json"
        elsewhere.write_bytes(path.read_bytes())
        os.chmod(elsewhere, 0o600)
        path.unlink()
        path.symlink_to(elsewhere)
        self.assert_refused(
            "declaration_permissions",
            lambda: posture_from_declaration(self.state_root, now_ms=NOW),
        )

    def test_declared_permissions_are_private(self) -> None:
        path = self.write(self.declaration())
        self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))


class DoctorGateTests(SandboxTestCase):
    """The gate must never let a disabled sandbox read as a pass."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_root = Path(temporary.name).resolve() / "state"
        (self.state_root / "oracle").mkdir(parents=True)
        os.chmod(self.state_root / "oracle", 0o700)
        self.ctx = SD.DoctorContext(
            runtime_root=ROOT_DIR, config_root=None, cwd=ROOT_DIR
        )

    def run_gate(self, document: object | None) -> tuple[str, str]:
        if document is not None:
            path = declaration_path(self.state_root)
            path.write_text(json.dumps(document), encoding="utf-8")
            os.chmod(path, 0o600)
        with mock.patch.dict(
            os.environ, {"SKILLBOX_STATE_ROOT": str(self.state_root)}
        ):
            return SD._run_oracle_browser_sandbox(self.ctx)

    def declaration(self, **overrides: object) -> dict[str, object]:
        # The gate reads the wall clock, so its waiver must be live right now.
        return declaration_document(int(time.time() * 1000), **overrides)

    def test_a_box_without_a_declaration_is_inconclusive(self) -> None:
        status, detail = self.run_gate(None)
        self.assertEqual(SD.STATUS_INCO, status)
        self.assertIn("not the oracle host", detail)

    def test_a_waived_host_is_inconclusive_and_never_a_pass(self) -> None:
        status, detail = self.run_gate(self.declaration())
        self.assertEqual(SD.STATUS_INCO, status)
        self.assertNotEqual(SD.STATUS_PASS, status)
        self.assertIn("DISABLED", detail)

    def test_an_uncontained_host_fails(self) -> None:
        document = self.declaration()
        del document["waiver"]
        status, detail = self.run_gate(document)
        self.assertEqual(SD.STATUS_FAIL, status)
        self.assertIn("NOT contained", detail)

    def test_an_enforced_host_passes(self) -> None:
        status, detail = self.run_gate(
            self.declaration(
                evidence={
                    "no_sandbox_flag": False,
                    "user_namespaces_available": True,
                    "setuid_sandbox_present": False,
                }
            )
        )
        self.assertEqual(SD.STATUS_PASS, status)
        self.assertIn("enforced", detail)

    def test_a_broken_declaration_fails_rather_than_going_inconclusive(self) -> None:
        path = declaration_path(self.state_root)
        path.write_bytes(b"{not json")
        os.chmod(path, 0o600)
        with mock.patch.dict(
            os.environ, {"SKILLBOX_STATE_ROOT": str(self.state_root)}
        ):
            status, detail = SD._run_oracle_browser_sandbox(self.ctx)
        self.assertEqual(SD.STATUS_FAIL, status)
        self.assertIn("declaration_invalid", detail)

    def test_the_gate_is_registered_as_a_bounded_structure_gate(self) -> None:
        specs = {spec.name: spec for spec in SD._gate_specs()}
        self.assertIn("oracle_browser_sandbox", specs)
        spec = specs["oracle_browser_sandbox"]
        self.assertEqual(SD.KIND_STRUCTURE, spec.kind)
        self.assertLessEqual(spec.cap_s, 60)
        self.assertIn("docs/oracle-sandbox.md", spec.fix_command)


class ContractTests(SandboxTestCase):
    """Invariants that keep the posture contract honest as it changes."""

    def test_every_refusal_code_in_the_source_is_declared(self) -> None:
        source = SANDBOX_SOURCE.read_text(encoding="utf-8")
        used = set(re.findall(r'_refuse\("([a-z_]+)"\)', source))
        self.assertTrue(used)
        self.assertEqual(set(), used - REFUSAL_CODES)

    def test_there_are_exactly_four_required_controls(self) -> None:
        # The bead names four; a silent reduction would weaken every waiver
        # already accepted under the old set.
        self.assertEqual(4, len(REQUIRED_CONTROLS))
        self.assertEqual(len(set(REQUIRED_CONTROLS)), len(REQUIRED_CONTROLS))

    def test_only_the_enforced_state_maps_to_a_pass_verdict(self) -> None:
        from runtime_manager.oracle_sandbox import _STATE_VERDICTS

        passing = {
            state for state, verdict in _STATE_VERDICTS.items() if verdict == VERDICT_PASS
        }
        self.assertEqual({STATE_ENFORCED}, passing)

    def test_refusals_share_the_oracle_error_surface(self) -> None:
        error = self.assert_refused(
            "sandbox_input_invalid",
            lambda: evaluate_sandbox_posture(
                None, controls(), None, now_ms=NOW, host=HOST
            ),
        )
        self.assertIsInstance(error, OracleBrokerError)
        self.assertEqual("sandbox_input_invalid", error.to_payload()["error_code"])

    def test_the_posture_payload_carries_no_host_detail(self) -> None:
        posture = evaluate_sandbox_posture(
            evidence(True), controls(), waiver(), now_ms=NOW, host=HOST
        )
        rendered = json.dumps(posture.to_payload())
        self.assertNotIn("/home/", rendered)
        self.assertNotIn("/Users/", rendered)


if __name__ == "__main__":
    unittest.main()
