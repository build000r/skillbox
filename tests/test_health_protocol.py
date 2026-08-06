"""Contract tests for the canonical read-only health-provider protocol.

The fixtures are deliberately modelled on the THREE providers the protocol has
to federate without losing evidence:

* ``runtime_manager.structure_doctor.GateResult``
  ``{name, kind, status(PASS|FAIL|INCO), duration_s, fix_command, detail}`` —
  including the two DISTINCT causes structure_doctor collapses into ``INCO``
  (cap exceeded vs. dependency unreachable), which the protocol splits into
  ``timed_out`` and ``unavailable``.
* the runtime-evidence packet sections (``evidence.collect_runtime_evidence``) —
  ``status``/``next_actions``/``blocked_conditions``/``last_tick_age_s``.
* ``scripts/04-reconcile.py`` ``CheckResult``
  ``{status(pass|warn|fail), code, message, details, fix_command}`` — which has
  no timing field at all.

Covered: pass/warn/fail/unavailable/timed-out fixtures, stable identity, scope,
severity-independent-of-status, freshness, provenance, duration, typed
next-action metadata, deterministic single-primary prioritization, and the
hard "fix_command is display text, never an execution path" guarantee (asserted
against the module's own AST, not just its behaviour).
"""
from __future__ import annotations

import ast
import json
import random
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import health_protocol as HP  # noqa: E402
from runtime_manager.health_protocol import (  # noqa: E402
    ACTION_ESCALATE,
    ACTION_INSPECT,
    ACTION_INSTALL_DEPENDENCY,
    ACTION_NONE,
    ACTION_REPAIR,
    ACTION_RETRY,
    NO_ACTION,
    OVERALL_GREEN,
    OVERALL_RED,
    OVERALL_YELLOW,
    SCOPE_REPO,
    SCOPE_RUNTIME,
    SCOPE_STRUCTURE,
    SEVERITY_ADVISORY,
    SEVERITY_CRITICAL,
    SEVERITY_NONE,
    SEVERITY_UNKNOWN,
    SEVERITY_WARNING,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_TIMED_OUT,
    STATUS_UNAVAILABLE,
    STATUS_WARN,
    CheckScope,
    HealthCheckResult,
    HealthProvider,
    NextAction,
    Prioritization,
    Provenance,
    ProviderDescriptor,
    federation_payload,
    fold_status,
    overall_light,
    prioritize,
    status_counts,
)

MODULE_PATH = ENV_MANAGER_DIR / "runtime_manager" / "health_protocol.py"

# A fixed "now" so every freshness assertion is deterministic.
NOW = 1_760_000_000.0


# --------------------------------------------------------------------------- #
# Fixtures — one per provider shape, covering every status in the vocabulary
# --------------------------------------------------------------------------- #


def structure_gate_pass() -> HealthCheckResult:
    """structure_doctor gate that PASSED but reported advisory warnings.

    structure_doctor folds advisory warns into PASS ("N advisory warning(s); no
    failures"), so severity must be expressible independently of status.
    """
    return HealthCheckResult(
        check_id="skill_drift",
        provider_id="structure_doctor",
        scope=CheckScope(kind=SCOPE_STRUCTURE, target=str(ROOT_DIR), labels=("gate",)),
        status=STATUS_PASS,
        severity=SEVERITY_ADVISORY,
        observed_at=NOW - 2.0,
        provenance=Provenance(
            provider_id="structure_doctor",
            source="runtime_manager.structure_doctor:_run_skill_drift",
            collector="sbp doctor --format json",
            evidence_ref="gates[skill_drift]",
        ),
        summary="no broken or missing skill links",
        detail="no broken or missing skill links (3 advisory drift item(s) — see sbp recalibrate)",
        duration_s=0.412,
        next_action=NextAction(
            action_id="structure_doctor.skill_drift.recalibrate",
            kind=ACTION_INSPECT,
            summary="review skill add/remove for this cwd",
            fix_command="sbp recalibrate  # review skill add/remove for this cwd",
        ),
    )


def structure_gate_fail() -> HealthCheckResult:
    """structure_doctor gate that RAN and reported a real failure."""
    return HealthCheckResult(
        check_id="lock_parity",
        provider_id="structure_doctor",
        scope=CheckScope(kind=SCOPE_STRUCTURE, target=str(ROOT_DIR), labels=("gate",)),
        status=STATUS_FAIL,
        severity=SEVERITY_CRITICAL,
        observed_at=NOW - 1.0,
        provenance=Provenance(
            provider_id="structure_doctor",
            source="runtime_manager.structure_doctor:_run_lock_parity",
            collector="sbp doctor --format json",
            evidence_ref="gates[lock_parity]",
        ),
        summary="config_sha desync in 1 skill repo",
        detail="skill-repo-lock: skill-repos.lock config_sha does not match skill-repos.yaml",
        duration_s=0.087,
        next_action=NextAction(
            action_id="structure_doctor.lock_parity.sync",
            kind=ACTION_REPAIR,
            summary="rewrite each lockfile's config_sha from its skill-repos.yaml",
            fix_command=(
                "cd ~/repos/opensource/skillbox/.env-manager && python3 manage.py sync"
            ),
        ),
    )


def structure_gate_timed_out() -> HealthCheckResult:
    """structure_doctor INCO variant #1: the gate exceeded its wall-clock cap.

    structure_doctor reports this as INCO with the detail "exceeded 45s cap —
    INCONCLUSIVE (not a failure)". The protocol keeps it OUT of pass and fail and
    preserves both the cause and the cap that produced it.
    """
    return HealthCheckResult(
        check_id="structure_invariants",
        provider_id="structure_doctor",
        scope=CheckScope(kind=SCOPE_STRUCTURE, target=str(ROOT_DIR), labels=("gate",)),
        status=STATUS_TIMED_OUT,
        severity=SEVERITY_UNKNOWN,
        observed_at=NOW - 46.0,
        provenance=Provenance(
            provider_id="structure_doctor",
            source="runtime_manager.structure_doctor:_run_structure_invariant_suite",
            collector="sbp doctor --format json",
            evidence_ref="gates[structure_invariants]",
        ),
        summary="structure invariant suite did not finish",
        detail="exceeded 45s cap — INCONCLUSIVE (not a failure)",
        duration_s=45.002,
        timeout_s=45.0,
        cause="exceeded the 45s per-gate cap on a loaded box; no verdict produced",
        next_action=NextAction(
            action_id="structure_doctor.structure_invariants.retry",
            kind=ACTION_RETRY,
            summary="re-run the gate on an unloaded box before treating it as drift",
            fix_command="sbp doctor --format json  # re-run; INCO is not a regression",
        ),
    )


def structure_gate_unavailable() -> HealthCheckResult:
    """structure_doctor INCO variant #2: the dependency is not on this box.

    Same INCO status upstream, categorically different meaning: the gate CANNOT
    run here. Folding it together with the timeout would destroy the distinction
    an operator needs to act.
    """
    return HealthCheckResult(
        check_id="runtime_doctor",
        provider_id="structure_doctor",
        scope=CheckScope(kind=SCOPE_RUNTIME, target=str(ROOT_DIR), labels=("gate",)),
        status=STATUS_UNAVAILABLE,
        severity=SEVERITY_UNKNOWN,
        observed_at=NOW - 0.5,
        provenance=Provenance(
            provider_id="structure_doctor",
            source="runtime_manager.structure_doctor:_run_runtime_doctor",
            collector="sbp doctor --format json",
            evidence_ref="gates[runtime_doctor]",
        ),
        summary="runtime doctor could not be reached",
        detail="make is not available on this box",
        duration_s=0.004,
        cause="make is not available on this box; the runtime verdict is unknown",
        next_action=NextAction(
            action_id="structure_doctor.runtime_doctor.install_make",
            kind=ACTION_INSTALL_DEPENDENCY,
            summary="install make (or run from a box that has the runtime toolchain)",
            fix_command="make doctor  # from ~/repos/opensource/skillbox",
        ),
    )


def evidence_section_warn() -> HealthCheckResult:
    """A runtime-evidence section: multiple next_actions + blocked conditions.

    ``_doctor_section`` emits ``status``, counters, and a LIST of next actions;
    the packet emits ``blocked_conditions``. All of it has to survive.
    """
    return HealthCheckResult(
        check_id="doctor",
        provider_id="runtime_evidence",
        scope=CheckScope(
            kind=SCOPE_RUNTIME,
            target=str(ROOT_DIR),
            labels=("profile:core", "section"),
        ),
        status=STATUS_WARN,
        severity=SEVERITY_WARNING,
        observed_at=NOW - 5.0,
        provenance=Provenance(
            provider_id="runtime_evidence",
            source="runtime_manager.evidence:_doctor_section",
            collector="python3 .env-manager/manage.py doctor --format json",
            evidence_ref="sections.doctor",
        ),
        summary="2 advisory warning(s)",
        detail="doctor: 2 warning(s): skill-repo-install, storage-posture",
        duration_s=1.204,
        next_action=NextAction(
            action_id="runtime_evidence.doctor.sync",
            kind=ACTION_REPAIR,
            summary="reconcile the runtime after reviewing the warnings",
            fix_command="sync --format json",
        ),
        related_actions=(
            NextAction(
                action_id="runtime_evidence.doctor.status",
                kind=ACTION_INSPECT,
                summary="re-read runtime status after the sync",
                fix_command="status --format json",
            ),
        ),
        blocked_conditions=(
            "doctor: 2 warning(s): skill-repo-install, storage-posture",
            "git: 3 uncommitted path(s)",
        ),
        details={"total": 21, "pass": 19, "warn": 2, "fail": 0},
    )


def evidence_section_unavailable_stale() -> HealthCheckResult:
    """The pulse section when its state file is unreadable, and it is STALE.

    ``_pulse_section`` returns ``{"state": "unreadable", ..., "error": ...}`` and
    elsewhere reports ``last_tick_age_s``. Freshness plus an unavailable cause is
    exactly this case.
    """
    return HealthCheckResult(
        check_id="pulse",
        provider_id="runtime_evidence",
        scope=CheckScope(kind=SCOPE_RUNTIME, target=str(ROOT_DIR), labels=("section",)),
        status=STATUS_UNAVAILABLE,
        severity=SEVERITY_UNKNOWN,
        observed_at=NOW - 900.0,
        max_age_s=300.0,
        provenance=Provenance(
            provider_id="runtime_evidence",
            source="runtime_manager.evidence:_pulse_section",
            collector="python3 .env-manager/manage.py status --format json",
            evidence_ref="sections.pulse",
        ),
        summary="pulse state file is unreadable",
        detail="state=unreadable",
        cause="Expecting value: line 1 column 1 (char 0) while reading pulse.state.json",
        next_action=NextAction(
            action_id="runtime_evidence.pulse.inspect",
            kind=ACTION_INSPECT,
            summary="read the pulse state file directly",
            fix_command="cat .skillbox-state/logs/runtime/pulse.state.json",
        ),
        details={"state_file_present": True, "last_tick_age_s": 900.0},
    )


def reconcile_check_fail() -> HealthCheckResult:
    """An outer reconcile ``CheckResult`` — free-form details, NO duration field.

    ``duration_s`` stays ``None`` because reconcile never measured one; writing
    ``0.0`` would invent a measurement.
    """
    return HealthCheckResult(
        check_id="expected-files",
        provider_id="outer_reconcile",
        scope=CheckScope(kind=SCOPE_REPO, target=str(ROOT_DIR), labels=("reconcile",)),
        status=STATUS_FAIL,
        severity=SEVERITY_CRITICAL,
        observed_at=NOW - 12.0,
        provenance=Provenance(
            provider_id="outer_reconcile",
            source="scripts/04-reconcile.py:check_required_files",
            collector="python3 scripts/04-reconcile.py doctor --format json",
            evidence_ref="checks[expected-files]",
        ),
        summary="2 expected file(s) missing",
        detail="missing: docker-compose.yml, .env.example",
        duration_s=None,
        next_action=NextAction(
            action_id="outer_reconcile.expected-files.render",
            kind=ACTION_REPAIR,
            summary="re-render the manifest-derived files",
            fix_command="make render",
        ),
        details={
            "missing": ["docker-compose.yml", ".env.example"],
            "checked": 17,
            "reason": {
                "docker-compose.yml": "manifest-validated compose file",
                ".env.example": "manifest-validated env template",
            },
        },
    )


def reconcile_check_pass() -> HealthCheckResult:
    """A clean reconcile check: no action, no cause, no duration."""
    return HealthCheckResult(
        check_id="manifest-alignment",
        provider_id="outer_reconcile",
        scope=CheckScope(kind=SCOPE_REPO, target=str(ROOT_DIR), labels=("reconcile",)),
        status=STATUS_PASS,
        severity=SEVERITY_NONE,
        observed_at=NOW - 12.0,
        provenance=Provenance(
            provider_id="outer_reconcile",
            source="scripts/04-reconcile.py:check_manifest_alignment",
            collector="python3 scripts/04-reconcile.py doctor --format json",
            evidence_ref="checks[manifest-alignment]",
        ),
        summary="manifest files agree on runtime paths",
        next_action=NO_ACTION,
    )


def all_fixtures() -> list[HealthCheckResult]:
    return [
        structure_gate_pass(),
        structure_gate_fail(),
        structure_gate_timed_out(),
        structure_gate_unavailable(),
        evidence_section_warn(),
        evidence_section_unavailable_stale(),
        reconcile_check_fail(),
        reconcile_check_pass(),
    ]


class _FakeProvider:
    """A minimal in-test provider. No I/O; proves the Protocol is satisfiable."""

    def describe(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            provider_id="structure_doctor",
            title="structural verification gates",
            scope_kinds=(SCOPE_STRUCTURE, SCOPE_RUNTIME),
            default_max_age_s=300.0,
        )

    def collect(self):
        return (structure_gate_pass(), structure_gate_fail())


# --------------------------------------------------------------------------- #
# Fixture coverage
# --------------------------------------------------------------------------- #


class FixtureCoverageTests(unittest.TestCase):
    def test_fixtures_cover_every_status_in_the_vocabulary(self) -> None:
        observed = {r.status for r in all_fixtures()}
        self.assertEqual(observed, set(HP.HEALTH_STATUSES))

    def test_fixtures_cover_all_three_existing_providers(self) -> None:
        providers = {r.provider_id for r in all_fixtures()}
        self.assertEqual(
            providers, {"structure_doctor", "runtime_evidence", "outer_reconcile"}
        )

    def test_identity_is_stable_and_unique_per_provider(self) -> None:
        keys = [(r.provider_id, r.check_id) for r in all_fixtures()]
        self.assertEqual(len(keys), len(set(keys)))
        # Rebuilding a fixture yields the identical identity + payload.
        first = structure_gate_fail()
        second = structure_gate_fail()
        self.assertEqual(first.check_id, second.check_id)
        self.assertEqual(first.to_payload(NOW), second.to_payload(NOW))


# --------------------------------------------------------------------------- #
# Core field contract
# --------------------------------------------------------------------------- #


class ResultContractTests(unittest.TestCase):
    def test_payload_carries_every_required_field(self) -> None:
        payload = structure_gate_fail().to_payload(NOW)
        self.assertEqual(
            set(payload),
            {
                "check_id",
                "provider_id",
                "scope",
                "status",
                "severity",
                "freshness",
                "duration_s",
                "timeout_s",
                "cause",
                "summary",
                "detail",
                "provenance",
                "next_action",
                "related_actions",
                "blocked_conditions",
                "details",
            },
        )
        self.assertEqual(set(payload["freshness"]), {"observed_at", "age_s", "max_age_s", "stale"})
        self.assertEqual(payload["scope"]["kind"], SCOPE_STRUCTURE)
        self.assertEqual(payload["provenance"]["provider_id"], "structure_doctor")

    def test_payload_is_json_serializable(self) -> None:
        encoded = json.dumps(federation_payload(all_fixtures(), NOW), sort_keys=True)
        self.assertIn('"health-federation"', encoded)

    def test_payload_round_trips_without_losing_provider_evidence(self) -> None:
        for original in all_fixtures():
            with self.subTest(check=original.check_id):
                restored = HealthCheckResult.from_payload(original.to_payload(NOW))
                self.assertEqual(restored.to_payload(NOW), original.to_payload(NOW))

    def test_reconcile_free_form_details_survive_verbatim(self) -> None:
        # EXTENSION 4: reconcile's CheckResult.details is provider-defined.
        result = reconcile_check_fail()
        self.assertEqual(
            result.details["reason"]["docker-compose.yml"],
            "manifest-validated compose file",
        )
        restored = HealthCheckResult.from_payload(result.to_payload(NOW))
        self.assertEqual(restored.details, result.details)

    def test_duration_is_optional_and_never_fabricated(self) -> None:
        # EXTENSION 2: outer reconcile has no timing field at all.
        self.assertIsNone(reconcile_check_fail().duration_s)
        self.assertEqual(structure_gate_fail().duration_s, 0.087)

    def test_severity_is_independent_of_status(self) -> None:
        # EXTENSION 1: structure_doctor folds advisory warns into PASS.
        passing = structure_gate_pass()
        self.assertEqual(passing.status, STATUS_PASS)
        self.assertEqual(passing.severity, SEVERITY_ADVISORY)

    def test_evidence_related_actions_and_blocked_conditions_survive(self) -> None:
        # EXTENSIONS 3 + 5: sections carry a LIST of next_actions, and the packet
        # carries blocked/gray conditions that belong to no single verdict.
        section = evidence_section_warn()
        self.assertEqual(len(section.all_actions), 2)
        self.assertEqual(
            [a.action_id for a in section.all_actions],
            ["runtime_evidence.doctor.sync", "runtime_evidence.doctor.status"],
        )
        self.assertIn("git: 3 uncommitted path(s)", section.blocked_conditions)

    def test_details_and_collections_are_defensively_copied(self) -> None:
        source = {"missing": ["a"]}
        result = HealthCheckResult(
            check_id="x",
            provider_id="p",
            scope=CheckScope(kind=SCOPE_REPO),
            status=STATUS_PASS,
            severity=SEVERITY_NONE,
            observed_at=NOW,
            provenance=Provenance(provider_id="p", source="s"),
            details=source,
        )
        source["missing"].append("b")
        self.assertEqual(result.details["missing"], ["a", "b"])  # shallow by design
        source["extra"] = 1
        self.assertNotIn("extra", result.details)

    def test_rejects_unknown_status_severity_and_scope(self) -> None:
        base = dict(
            check_id="x",
            provider_id="p",
            scope=CheckScope(kind=SCOPE_REPO),
            status=STATUS_PASS,
            severity=SEVERITY_NONE,
            observed_at=NOW,
            provenance=Provenance(provider_id="p", source="s"),
        )
        with self.assertRaises(ValueError):
            HealthCheckResult(**{**base, "status": "INCO"})
        with self.assertRaises(ValueError):
            HealthCheckResult(**{**base, "severity": "spicy"})
        with self.assertRaises(ValueError):
            CheckScope(kind="galaxy")

    def test_rejects_missing_identity_and_mismatched_provenance(self) -> None:
        with self.assertRaises(ValueError):
            HealthCheckResult(
                check_id="  ",
                provider_id="p",
                scope=CheckScope(kind=SCOPE_REPO),
                status=STATUS_PASS,
                severity=SEVERITY_NONE,
                observed_at=NOW,
                provenance=Provenance(provider_id="p", source="s"),
            )
        with self.assertRaises(ValueError):
            HealthCheckResult(
                check_id="x",
                provider_id="p",
                scope=CheckScope(kind=SCOPE_REPO),
                status=STATUS_PASS,
                severity=SEVERITY_NONE,
                observed_at=NOW,
                provenance=Provenance(provider_id="other", source="s"),
            )
        with self.assertRaises(ValueError):
            Provenance(provider_id="p", source="")


# --------------------------------------------------------------------------- #
# unavailable / timed_out are distinct states
# --------------------------------------------------------------------------- #


class UnknownStatusTests(unittest.TestCase):
    def test_unavailable_and_timed_out_are_neither_pass_nor_fail(self) -> None:
        for result in (structure_gate_unavailable(), structure_gate_timed_out()):
            with self.subTest(status=result.status):
                self.assertNotIn(result.status, (STATUS_PASS, STATUS_FAIL, STATUS_WARN))
                self.assertTrue(result.is_unknown)
                self.assertIn(result.status, HP.UNKNOWN_STATUSES)

    def test_unavailable_and_timed_out_are_distinct_from_each_other(self) -> None:
        # structure_doctor collapses both into INCO; the protocol must not.
        self.assertNotEqual(structure_gate_unavailable().status, structure_gate_timed_out().status)
        self.assertEqual(structure_gate_unavailable().status, STATUS_UNAVAILABLE)
        self.assertEqual(structure_gate_timed_out().status, STATUS_TIMED_OUT)

    def test_unknown_states_preserve_cause_and_provenance(self) -> None:
        timed_out = structure_gate_timed_out()
        self.assertIn("45s per-gate cap", timed_out.cause)
        self.assertEqual(timed_out.timeout_s, 45.0)
        self.assertEqual(
            timed_out.provenance.source,
            "runtime_manager.structure_doctor:_run_structure_invariant_suite",
        )
        unavailable = structure_gate_unavailable()
        self.assertIn("make is not available", unavailable.cause)
        self.assertEqual(unavailable.provenance.collector, "sbp doctor --format json")
        # The cause + provenance survive a serialization round trip.
        payload = timed_out.to_payload(NOW)
        self.assertEqual(payload["cause"], timed_out.cause)
        self.assertEqual(payload["provenance"], timed_out.provenance.to_payload())

    def test_unknown_state_without_a_cause_is_rejected(self) -> None:
        for status in (STATUS_UNAVAILABLE, STATUS_TIMED_OUT):
            with self.subTest(status=status), self.assertRaises(ValueError):
                HealthCheckResult(
                    check_id="x",
                    provider_id="p",
                    scope=CheckScope(kind=SCOPE_REPO),
                    status=status,
                    severity=SEVERITY_UNKNOWN,
                    observed_at=NOW,
                    provenance=Provenance(provider_id="p", source="s"),
                )

    def test_pass_may_not_carry_a_cause(self) -> None:
        with self.assertRaises(ValueError):
            HealthCheckResult(
                check_id="x",
                provider_id="p",
                scope=CheckScope(kind=SCOPE_REPO),
                status=STATUS_PASS,
                severity=SEVERITY_NONE,
                observed_at=NOW,
                provenance=Provenance(provider_id="p", source="s"),
                cause="nope",
            )

    def test_unknown_states_never_read_as_green(self) -> None:
        self.assertEqual(overall_light(STATUS_UNAVAILABLE), OVERALL_YELLOW)
        self.assertEqual(overall_light(STATUS_TIMED_OUT), OVERALL_YELLOW)
        self.assertEqual(overall_light(STATUS_FAIL), OVERALL_RED)
        self.assertEqual(overall_light(STATUS_PASS), OVERALL_GREEN)


# --------------------------------------------------------------------------- #
# Freshness
# --------------------------------------------------------------------------- #


class FreshnessTests(unittest.TestCase):
    def test_age_is_measured_from_observed_at(self) -> None:
        self.assertEqual(structure_gate_fail().age_s(NOW), 1.0)
        self.assertEqual(evidence_section_warn().age_s(NOW), 5.0)

    def test_clock_skew_clamps_to_zero_rather_than_going_negative(self) -> None:
        self.assertEqual(structure_gate_fail().age_s(NOW - 60.0), 0.0)

    def test_result_without_max_age_never_goes_stale(self) -> None:
        self.assertIsNone(structure_gate_fail().max_age_s)
        self.assertFalse(structure_gate_fail().is_stale(NOW + 10_000.0))

    def test_result_past_max_age_is_stale(self) -> None:
        stale = evidence_section_unavailable_stale()
        self.assertEqual(stale.age_s(NOW), 900.0)
        self.assertTrue(stale.is_stale(NOW))
        self.assertFalse(stale.is_stale(NOW - 700.0))
        self.assertTrue(stale.freshness(NOW)["stale"])


# --------------------------------------------------------------------------- #
# Typed next-action metadata + the display-only safety contract
# --------------------------------------------------------------------------- #


class NextActionTests(unittest.TestCase):
    def test_actions_are_typed_with_a_known_kind(self) -> None:
        kinds = {r.next_action.kind for r in all_fixtures()}
        self.assertLessEqual(kinds, set(HP.ACTION_KINDS))
        self.assertIn(ACTION_INSTALL_DEPENDENCY, kinds)
        self.assertIn(ACTION_RETRY, kinds)
        self.assertIn(ACTION_REPAIR, kinds)
        self.assertIn(ACTION_INSPECT, kinds)
        self.assertIn(ACTION_NONE, kinds)

    def test_every_action_is_non_executable_and_operator_decided(self) -> None:
        for result in all_fixtures():
            action = result.next_action
            with self.subTest(check=result.check_id):
                self.assertFalse(action.executable)
                self.assertTrue(action.requires_human)
                self.assertFalse(action.to_payload()["executable"])

    def test_action_rejects_a_non_human_gated_action(self) -> None:
        with self.assertRaises(ValueError):
            NextAction(
                action_id="auto",
                kind=ACTION_REPAIR,
                fix_command="rm -rf /",
                requires_human=False,
            )

    def test_none_action_may_not_carry_a_fix_command(self) -> None:
        with self.assertRaises(ValueError):
            NextAction(action_id="x", kind=ACTION_NONE, fix_command="make render")
        self.assertEqual(NO_ACTION.fix_command, "")
        self.assertFalse(NO_ACTION.is_actionable)

    def test_action_rejects_unknown_kind_and_blank_id(self) -> None:
        with self.assertRaises(ValueError):
            NextAction(action_id="x", kind="yolo")
        with self.assertRaises(ValueError):
            NextAction(action_id=" ", kind=ACTION_ESCALATE)

    def test_module_contains_no_execution_primitive(self) -> None:
        """The strongest form of "fix_command is never executable".

        Asserted against the module's own AST so the guarantee cannot regress by
        someone later importing subprocess and shelling out to a fix_command.
        """
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        forbidden_modules = {
            "subprocess",
            "os",
            "shutil",
            "pty",
            "popen2",
            "commands",
            "asyncio",
            "multiprocessing",
            "socket",
            "shlex",
            "pathlib",
        }
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(
            sorted(imported & forbidden_modules),
            [],
            "health_protocol must stay pure: no execution or filesystem imports",
        )

        forbidden_calls = {"eval", "exec", "compile", "system", "popen", "spawn", "run"}
        called: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
        self.assertEqual(
            sorted(called & forbidden_calls),
            [],
            "health_protocol must never invoke an execution primitive",
        )

    def test_fix_command_strings_are_only_ever_data(self) -> None:
        # A fix_command is carried, rendered, and round-tripped as text — the
        # module exposes no call site that consumes one.
        result = structure_gate_fail()
        payload = result.to_payload(NOW)
        self.assertEqual(
            payload["next_action"]["fix_command"],
            "cd ~/repos/opensource/skillbox/.env-manager && python3 manage.py sync",
        )
        self.assertIsInstance(payload["next_action"]["fix_command"], str)


# --------------------------------------------------------------------------- #
# Deterministic prioritization
# --------------------------------------------------------------------------- #


class PrioritizerTests(unittest.TestCase):
    def test_emits_at_most_one_primary_action(self) -> None:
        result = prioritize(all_fixtures(), NOW)
        self.assertIsInstance(result, Prioritization)
        self.assertIsNotNone(result.primary)
        self.assertIsInstance(result.primary.action, NextAction)
        # `primary` is a single object, not a list — one action, structurally.
        self.assertFalse(isinstance(result.primary, (list, tuple)))

    def test_primary_is_the_highest_ranked_failure(self) -> None:
        result = prioritize(all_fixtures(), NOW)
        # Two criticals FAIL; ties break on provider_id then check_id, so
        # outer_reconcile/expected-files loses to structure_doctor/lock_parity.
        self.assertEqual(result.primary.provider_id, "outer_reconcile")
        self.assertEqual(result.primary.check_id, "expected-files")
        self.assertEqual(result.primary.status, STATUS_FAIL)
        self.assertIn("outranks", result.primary.rationale)

    def test_ordering_is_status_then_severity_then_freshness(self) -> None:
        ordered = sorted(
            (r for r in all_fixtures() if r.is_actionable),
            key=lambda r: HP.priority_key(r, NOW),
        )
        self.assertEqual(
            [(r.provider_id, r.check_id) for r in ordered],
            [
                ("outer_reconcile", "expected-files"),
                ("structure_doctor", "lock_parity"),
                ("structure_doctor", "structure_invariants"),
                # both unavailable+unknown; the STALE one is demoted
                ("structure_doctor", "runtime_doctor"),
                ("runtime_evidence", "pulse"),
                ("runtime_evidence", "doctor"),
            ],
        )

    def test_fail_outranks_unknown_which_outranks_warn(self) -> None:
        subset = [
            evidence_section_warn(),
            structure_gate_unavailable(),
            structure_gate_fail(),
        ]
        self.assertEqual(prioritize(subset, NOW).primary.status, STATUS_FAIL)
        self.assertEqual(prioritize(subset[:2], NOW).primary.status, STATUS_UNAVAILABLE)
        self.assertEqual(prioritize(subset[:1], NOW).primary.status, STATUS_WARN)

    def test_fresh_finding_outranks_an_otherwise_identical_stale_one(self) -> None:
        def _warn(check_id: str, observed_at: float) -> HealthCheckResult:
            return HealthCheckResult(
                check_id=check_id,
                provider_id="p",
                scope=CheckScope(kind=SCOPE_REPO),
                status=STATUS_WARN,
                severity=SEVERITY_WARNING,
                observed_at=observed_at,
                max_age_s=60.0,
                provenance=Provenance(provider_id="p", source="s"),
                next_action=NextAction(
                    action_id=f"{check_id}.inspect", kind=ACTION_INSPECT
                ),
            )

        # The stale one sorts FIRST alphabetically, so only freshness can flip it.
        stale = _warn("aaa", NOW - 600.0)
        fresh = _warn("zzz", NOW - 1.0)
        picked = prioritize([stale, fresh], NOW)
        self.assertEqual(picked.primary.check_id, "zzz")
        self.assertFalse(picked.primary.stale)

    def test_prioritization_is_independent_of_input_order(self) -> None:
        expected = prioritize(all_fixtures(), NOW).to_payload()
        rng = random.Random(20260725)
        for _ in range(25):
            shuffled = all_fixtures()
            rng.shuffle(shuffled)
            self.assertEqual(prioritize(shuffled, NOW).to_payload(), expected)

    def test_secondary_entries_are_references_without_fix_commands(self) -> None:
        result = prioritize(all_fixtures(), NOW)
        self.assertTrue(result.secondary)
        for ref in result.secondary:
            with self.subTest(ref=ref.action_id):
                self.assertFalse(hasattr(ref, "fix_command"))
                self.assertNotIn("fix_command", ref.to_payload())
        # Exactly one copy-pasteable command exists in the whole payload.
        commands = [
            value
            for entry in [result.to_payload()]
            for value in json.dumps(entry).split('"fix_command": "')[1:]
        ]
        self.assertEqual(len(commands), 1)

    def test_secondary_includes_the_primary_checks_extra_actions(self) -> None:
        picked = prioritize([evidence_section_warn()], NOW)
        self.assertEqual(picked.primary.action.action_id, "runtime_evidence.doctor.sync")
        self.assertEqual(
            [ref.action_id for ref in picked.secondary],
            ["runtime_evidence.doctor.status"],
        )

    def test_passing_results_never_produce_an_action(self) -> None:
        picked = prioritize([reconcile_check_pass(), structure_gate_pass()], NOW)
        self.assertIsNone(picked.primary)
        self.assertEqual(picked.secondary, ())
        self.assertEqual(picked.considered, 0)
        self.assertEqual(picked.overall, OVERALL_GREEN)

    def test_empty_input_is_green_with_no_action(self) -> None:
        picked = prioritize([], NOW)
        self.assertIsNone(picked.primary)
        self.assertEqual(picked.overall, OVERALL_GREEN)
        self.assertEqual(picked.status_counts[STATUS_FAIL], 0)


# --------------------------------------------------------------------------- #
# Folding + federation payload
# --------------------------------------------------------------------------- #


class FederationTests(unittest.TestCase):
    def test_status_counts_cover_the_whole_vocabulary(self) -> None:
        counts = status_counts(all_fixtures())
        self.assertEqual(set(counts), set(HP.HEALTH_STATUSES))
        self.assertEqual(counts[STATUS_FAIL], 2)
        self.assertEqual(counts[STATUS_PASS], 2)
        self.assertEqual(counts[STATUS_UNAVAILABLE], 2)
        self.assertEqual(counts[STATUS_TIMED_OUT], 1)
        self.assertEqual(counts[STATUS_WARN], 1)

    def test_fold_reports_the_worst_status(self) -> None:
        self.assertEqual(fold_status(all_fixtures()), STATUS_FAIL)
        self.assertEqual(
            fold_status([structure_gate_pass(), structure_gate_unavailable()]),
            STATUS_UNAVAILABLE,
        )
        self.assertEqual(fold_status([]), STATUS_PASS)

    def test_federation_payload_shape_and_summary(self) -> None:
        payload = federation_payload(all_fixtures(), NOW)
        self.assertEqual(payload["kind"], "health-federation")
        self.assertEqual(len(payload["checks"]), 8)
        self.assertEqual(payload["summary"]["total"], 8)
        self.assertEqual(payload["summary"]["unknown"], 3)
        self.assertEqual(payload["summary"]["stale"], 1)
        self.assertEqual(payload["summary"]["overall"], OVERALL_RED)
        self.assertEqual(
            payload["prioritization"]["primary"]["check_id"], "expected-files"
        )

    def test_federation_payload_is_order_independent(self) -> None:
        expected = federation_payload(all_fixtures(), NOW)
        rng = random.Random(7)
        shuffled = all_fixtures()
        rng.shuffle(shuffled)
        self.assertEqual(federation_payload(shuffled, NOW), expected)


# --------------------------------------------------------------------------- #
# Provider interface
# --------------------------------------------------------------------------- #


class ProviderInterfaceTests(unittest.TestCase):
    def test_a_read_only_provider_satisfies_the_protocol(self) -> None:
        provider = _FakeProvider()
        self.assertIsInstance(provider, HealthProvider)
        descriptor = provider.describe()
        self.assertTrue(descriptor.read_only)
        self.assertEqual(descriptor.provider_id, "structure_doctor")
        self.assertEqual(len(provider.collect()), 2)

    def test_descriptor_rejects_a_mutating_provider(self) -> None:
        with self.assertRaises(ValueError):
            ProviderDescriptor(provider_id="p", title="t", read_only=False)

    def test_descriptor_rejects_unknown_scope_kinds(self) -> None:
        with self.assertRaises(ValueError):
            ProviderDescriptor(provider_id="p", title="t", scope_kinds=("galaxy",))

    def test_module_integrates_no_provider(self) -> None:
        # Non-goal guard: the protocol must not import any provider module.
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module or "")
        self.assertEqual(
            sorted(m for m in modules if m.startswith(".") or "runtime_manager" in m),
            [],
        )


if __name__ == "__main__":
    unittest.main()
