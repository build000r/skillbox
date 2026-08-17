"""Contract tests for Oracle browser receipt freshness and refresh.

Two things must be true at once, and the suite is organised around that
tension: a long Deep Research run must not lose a browser it never lost, and a
receipt with nothing behind it must still die at fifteen minutes. Every
loosening here is paid for by a proof of continued ownership.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.oracle_broker import OracleBrokerError  # noqa: E402
from runtime_manager.oracle_receipt import (  # noqa: E402
    FUTURE_SKEW_MS,
    MAX_OBSERVATIONS,
    MAX_RUN_SECONDS,
    ORACLE_RECEIPT_FRESHNESS_SCHEMA,
    RECEIPT_GRACE_MS,
    RECEIPT_MAX_AGE_MS,
    RECEIPT_MAX_LIFETIME_MS,
    RECEIPT_REFRESH_INTERVAL_MS,
    REFUSAL_CODES,
    STATE_BROKEN,
    STATE_FRESH,
    STATE_RENEWED,
    STATE_STALE,
    TRANSPORT_CHECK_KEYS,
    BrowserOwnership,
    BrowserReceipt,
    OracleReceiptError,
    OwnershipObservation,
    contract_payload,
    evaluate_receipt_freshness,
    receipt_lifetime_for_run,
)

RECEIPT_SOURCE = ENV_MANAGER_DIR / "runtime_manager" / "oracle_receipt.py"

#: The JS doctor this contract governs. Absent on a box that has not checked
#: out the skills repo, so the drift check skips rather than fails there.
JS_DOCTOR_CANDIDATES = (
    ROOT_DIR.parent
    / "skills"
    / "deep-research-prompt"
    / "assets"
    / "scripts"
    / "oracle-subagent-auth.mjs",
    ROOT_DIR
    / "workspace"
    / "skill-repos"
    / "build000r-skills"
    / "deep-research-prompt"
    / "assets"
    / "scripts"
    / "oracle-subagent-auth.mjs",
)

T0 = 1_700_000_000_000
MINUTE = 60 * 1000
DEEP_RESEARCH_SECONDS = 7200


def ownership(**overrides: object) -> BrowserOwnership:
    values: dict[str, object] = {
        "pid": 4242,
        "port": 9222,
        "target_id": "A1B2C3D4E5F60718",
        "profile_fingerprint": "0" * 64,
        "pid_start_token": "boot-9f2a:114217",
    }
    values.update(overrides)
    return BrowserOwnership(**values)  # type: ignore[arg-type]


def receipt(observed_at_ms: int = T0, **overrides: object) -> BrowserReceipt:
    return BrowserReceipt(
        observed_at_ms=observed_at_ms, ownership=ownership(**overrides)
    )


def refresh_chain(
    start_ms: int,
    through_ms: int,
    *,
    every_ms: int = RECEIPT_REFRESH_INTERVAL_MS,
    owner: BrowserOwnership | None = None,
) -> list[OwnershipObservation]:
    """A well-behaved refresher: one verified proof every interval."""
    owner = owner or ownership()
    stamps = range(start_ms + every_ms, through_ms + 1, every_ms)
    return [
        OwnershipObservation(observed_at_ms=stamp, ownership=owner) for stamp in stamps
    ]


class ReceiptTestCase(unittest.TestCase):
    def assert_refused(self, code: str, action: object) -> OracleReceiptError:
        with self.assertRaises(OracleReceiptError) as caught:
            action()  # type: ignore[operator]
        self.assertEqual(code, caught.exception.code)
        return caught.exception


class StaleRejectionPreservedTests(ReceiptTestCase):
    """The old ceiling still applies to any receipt with nothing behind it."""

    def test_unrefreshed_receipt_is_fresh_inside_the_window(self) -> None:
        verdict = evaluate_receipt_freshness(receipt(), now_ms=T0 + 14 * MINUTE)
        self.assertEqual(STATE_FRESH, verdict.state)
        self.assertTrue(verdict.usable)
        self.assertEqual((), verdict.reasons)
        self.assertEqual(0, verdict.refresh_count)

    def test_the_window_boundary_is_inclusive(self) -> None:
        # Mirrors the JS doctor's ageIsFresh, which is inclusive at the max.
        verdict = evaluate_receipt_freshness(
            receipt(), now_ms=T0 + RECEIPT_MAX_AGE_MS
        )
        self.assertEqual(STATE_FRESH, verdict.state)
        one_past = evaluate_receipt_freshness(
            receipt(), now_ms=T0 + RECEIPT_MAX_AGE_MS + 1
        )
        self.assertEqual(STATE_STALE, one_past.state)

    def test_unrefreshed_receipt_goes_stale_at_sixteen_minutes(self) -> None:
        verdict = evaluate_receipt_freshness(receipt(), now_ms=T0 + 16 * MINUTE)
        self.assertEqual(STATE_STALE, verdict.state)
        self.assertFalse(verdict.usable)
        self.assertEqual(("browser_receipt_stale",), verdict.reasons)

    def test_a_long_admitted_run_does_not_by_itself_extend_anything(self) -> None:
        # The alignment is not a blanket loosening: without proof of continued
        # ownership, a two-hour run still loses the browser at minute sixteen.
        verdict = evaluate_receipt_freshness(
            receipt(),
            now_ms=T0 + 16 * MINUTE,
            run_timeout_seconds=DEEP_RESEARCH_SECONDS,
        )
        self.assertEqual(STATE_STALE, verdict.state)
        self.assertEqual(("browser_receipt_stale",), verdict.reasons)

    def test_a_lapsed_refresher_goes_stale_even_mid_run(self) -> None:
        chain = refresh_chain(T0, T0 + 30 * MINUTE)
        verdict = evaluate_receipt_freshness(
            receipt(),
            chain,
            now_ms=T0 + 50 * MINUTE,
            run_timeout_seconds=DEEP_RESEARCH_SECONDS,
        )
        self.assertEqual(STATE_STALE, verdict.state)
        self.assertEqual(("browser_receipt_stale",), verdict.reasons)
        self.assertEqual(T0 + 30 * MINUTE, verdict.verified_through_ms)


class LongRunRegressionTests(ReceiptTestCase):
    """The bug this bead exists for: a two-hour run keeps its browser."""

    def test_a_two_hour_run_stays_usable_when_ownership_is_re_proven(self) -> None:
        end = T0 + DEEP_RESEARCH_SECONDS * 1000
        chain = refresh_chain(T0, end)
        verdict = evaluate_receipt_freshness(
            receipt(), chain, now_ms=end, run_timeout_seconds=DEEP_RESEARCH_SECONDS
        )
        self.assertEqual(STATE_RENEWED, verdict.state)
        self.assertTrue(verdict.usable)
        self.assertEqual((), verdict.reasons)
        self.assertEqual(DEEP_RESEARCH_SECONDS * 1000, verdict.age_ms)
        self.assertEqual(len(chain), verdict.refresh_count)

    def test_usable_at_every_point_across_the_whole_run(self) -> None:
        # A single end-of-run assertion could pass while the middle was broken.
        end = T0 + DEEP_RESEARCH_SECONDS * 1000
        chain = refresh_chain(T0, end)
        for minute in range(0, DEEP_RESEARCH_SECONDS // 60 + 1, 5):
            now = T0 + minute * MINUTE
            live = [obs for obs in chain if obs.observed_at_ms <= now]
            verdict = evaluate_receipt_freshness(
                receipt(),
                live,
                now_ms=now,
                run_timeout_seconds=DEEP_RESEARCH_SECONDS,
            )
            self.assertTrue(verdict.usable, f"minute {minute}: {verdict.reasons}")

    def test_the_grace_period_covers_a_run_that_ends_on_its_deadline(self) -> None:
        end = T0 + DEEP_RESEARCH_SECONDS * 1000
        chain = refresh_chain(T0, end)
        verdict = evaluate_receipt_freshness(
            receipt(),
            chain,
            now_ms=end + RECEIPT_GRACE_MS - 1,
            run_timeout_seconds=DEEP_RESEARCH_SECONDS,
        )
        self.assertTrue(verdict.usable)

    def test_renewal_is_hard_capped_by_the_run_lifetime(self) -> None:
        # Refreshing forever must not mean living forever.
        lifetime = receipt_lifetime_for_run(DEEP_RESEARCH_SECONDS)
        past_end = T0 + lifetime + MINUTE
        chain = refresh_chain(T0, past_end)
        verdict = evaluate_receipt_freshness(
            receipt(),
            chain,
            now_ms=past_end,
            run_timeout_seconds=DEEP_RESEARCH_SECONDS,
        )
        self.assertEqual(STATE_STALE, verdict.state)
        self.assertEqual(("browser_receipt_expired",), verdict.reasons)
        self.assertEqual(T0 + lifetime, verdict.expires_at_ms)

    def test_lifetime_tracks_the_admitted_run_and_is_bounded(self) -> None:
        self.assertEqual(RECEIPT_MAX_AGE_MS, receipt_lifetime_for_run(None))
        self.assertEqual(RECEIPT_MAX_AGE_MS, receipt_lifetime_for_run(60))
        self.assertEqual(
            DEEP_RESEARCH_SECONDS * 1000 + RECEIPT_GRACE_MS,
            receipt_lifetime_for_run(DEEP_RESEARCH_SECONDS),
        )
        self.assertEqual(
            RECEIPT_MAX_LIFETIME_MS, receipt_lifetime_for_run(MAX_RUN_SECONDS)
        )
        for value in (0, -1, MAX_RUN_SECONDS + 1, True, "7200", 7200.0):
            self.assert_refused(
                "doctor_input_invalid",
                lambda value=value: receipt_lifetime_for_run(value),
            )

    def test_the_longest_admissible_run_is_covered(self) -> None:
        end = T0 + MAX_RUN_SECONDS * 1000
        chain = refresh_chain(T0, end)
        verdict = evaluate_receipt_freshness(
            receipt(), chain, now_ms=end, run_timeout_seconds=MAX_RUN_SECONDS
        )
        self.assertTrue(verdict.usable)
        self.assertLessEqual(len(chain), MAX_OBSERVATIONS)


class OwnershipIsExactTests(ReceiptTestCase):
    """A different browser is never a freshness question."""

    def test_every_ownership_field_must_match_to_renew(self) -> None:
        drifts = (
            {"pid": 4243},
            {"port": 9223},
            {"target_id": "FFFFFFFFFFFFFFFF"},
            {"profile_fingerprint": "1" * 64},
            {"pid_start_token": "boot-9f2a:999999"},
        )
        for drift in drifts:
            chain = [
                OwnershipObservation(
                    observed_at_ms=T0 + 5 * MINUTE, ownership=ownership(**drift)
                )
            ]
            verdict = evaluate_receipt_freshness(
                receipt(), chain, now_ms=T0 + 6 * MINUTE
            )
            self.assertEqual(STATE_BROKEN, verdict.state, drift)
            self.assertEqual(("browser_identity_changed",), verdict.reasons)
            self.assertFalse(verdict.usable)

    def test_identity_change_breaks_even_a_young_receipt(self) -> None:
        # Age cannot rescue a browser that is provably not ours any more.
        chain = [
            OwnershipObservation(
                observed_at_ms=T0 + 1000, ownership=ownership(pid=4243)
            )
        ]
        verdict = evaluate_receipt_freshness(receipt(), chain, now_ms=T0 + 2000)
        self.assertEqual(STATE_BROKEN, verdict.state)
        self.assertFalse(verdict.usable)

    def test_a_failed_transport_check_breaks_the_chain(self) -> None:
        chain = [
            OwnershipObservation(
                observed_at_ms=T0 + 5 * MINUTE, ownership=ownership(), verified=False
            )
        ]
        verdict = evaluate_receipt_freshness(receipt(), chain, now_ms=T0 + 6 * MINUTE)
        self.assertEqual(STATE_BROKEN, verdict.state)
        self.assertEqual(("browser_ownership_unverified",), verdict.reasons)

    def test_from_transport_maps_the_doctor_booleans(self) -> None:
        verified = OwnershipObservation.from_transport(
            T0 + MINUTE, ownership(), dict.fromkeys(TRANSPORT_CHECK_KEYS, True)
        )
        self.assertTrue(verified.verified)
        for key in TRANSPORT_CHECK_KEYS:
            transport = dict.fromkeys(TRANSPORT_CHECK_KEYS, True)
            transport[key] = False
            observation = OwnershipObservation.from_transport(
                T0 + MINUTE, ownership(), transport
            )
            self.assertFalse(observation.verified, key)

    def test_from_transport_rejects_a_partial_or_untyped_report(self) -> None:
        self.assert_refused(
            "observation_invalid",
            lambda: OwnershipObservation.from_transport(T0, ownership(), {}),
        )
        self.assert_refused(
            "observation_invalid",
            lambda: OwnershipObservation.from_transport(
                T0, ownership(), dict.fromkeys(TRANSPORT_CHECK_KEYS, "yes")
            ),
        )
        self.assert_refused(
            "observation_invalid",
            lambda: OwnershipObservation.from_transport(T0, ownership(), None),
        )


class ContinuityTests(ReceiptTestCase):
    """A proof covers the interval it watched, and no more."""

    def test_a_gap_ends_the_chain_and_later_proofs_do_not_cover_it(self) -> None:
        owner = ownership()
        chain = [
            OwnershipObservation(observed_at_ms=T0 + 5 * MINUTE, ownership=owner),
            # 20-minute gap: nobody was watching.
            OwnershipObservation(observed_at_ms=T0 + 25 * MINUTE, ownership=owner),
            OwnershipObservation(observed_at_ms=T0 + 30 * MINUTE, ownership=owner),
        ]
        verdict = evaluate_receipt_freshness(
            receipt(),
            chain,
            now_ms=T0 + 31 * MINUTE,
            run_timeout_seconds=DEEP_RESEARCH_SECONDS,
        )
        self.assertEqual(STATE_STALE, verdict.state)
        self.assertEqual(1, verdict.refresh_count)
        self.assertEqual(T0 + 5 * MINUTE, verdict.verified_through_ms)

    def test_a_gap_exactly_at_the_interval_still_extends(self) -> None:
        owner = ownership()
        chain = [
            OwnershipObservation(
                observed_at_ms=T0 + RECEIPT_REFRESH_INTERVAL_MS, ownership=owner
            ),
            OwnershipObservation(
                observed_at_ms=T0 + 2 * RECEIPT_REFRESH_INTERVAL_MS, ownership=owner
            ),
        ]
        verdict = evaluate_receipt_freshness(
            receipt(), chain, now_ms=T0 + 20 * MINUTE, run_timeout_seconds=3600
        )
        self.assertEqual(STATE_RENEWED, verdict.state)
        self.assertEqual(2, verdict.refresh_count)

    def test_refresh_due_at_is_reported_so_a_refresher_can_act(self) -> None:
        chain = refresh_chain(T0, T0 + 10 * MINUTE)
        verdict = evaluate_receipt_freshness(
            receipt(), chain, now_ms=T0 + 11 * MINUTE, run_timeout_seconds=3600
        )
        self.assertEqual(
            T0 + 10 * MINUTE + RECEIPT_REFRESH_INTERVAL_MS, verdict.refresh_due_at_ms
        )

    def test_a_backwards_chain_is_refused(self) -> None:
        owner = ownership()
        chain = [
            OwnershipObservation(observed_at_ms=T0 + 10 * MINUTE, ownership=owner),
            OwnershipObservation(observed_at_ms=T0 + 5 * MINUTE, ownership=owner),
        ]
        self.assert_refused(
            "observation_out_of_order",
            lambda: evaluate_receipt_freshness(
                receipt(), chain, now_ms=T0 + 11 * MINUTE
            ),
        )

    def test_an_observation_before_the_receipt_is_refused(self) -> None:
        chain = [
            OwnershipObservation(observed_at_ms=T0 - MINUTE, ownership=ownership())
        ]
        self.assert_refused(
            "observation_out_of_order",
            lambda: evaluate_receipt_freshness(receipt(), chain, now_ms=T0 + MINUTE),
        )

    def test_a_future_observation_is_refused(self) -> None:
        chain = [
            OwnershipObservation(
                observed_at_ms=T0 + 10 * MINUTE + FUTURE_SKEW_MS + 1,
                ownership=ownership(),
            )
        ]
        self.assert_refused(
            "observation_invalid",
            lambda: evaluate_receipt_freshness(
                receipt(), chain, now_ms=T0 + 10 * MINUTE
            ),
        )

    def test_an_overlong_chain_is_refused(self) -> None:
        owner = ownership()
        chain = [
            OwnershipObservation(observed_at_ms=T0 + index, ownership=owner)
            for index in range(1, MAX_OBSERVATIONS + 2)
        ]
        self.assert_refused(
            "observation_overflow",
            lambda: evaluate_receipt_freshness(
                receipt(), chain, now_ms=T0 + MAX_OBSERVATIONS + 5
            ),
        )


class PidReuseDefenceTests(ReceiptTestCase):
    """Extension is only offered where pid reuse is detectable."""

    def test_a_receipt_without_a_start_token_cannot_be_extended(self) -> None:
        legacy = receipt(pid_start_token="")
        chain = refresh_chain(
            T0, T0 + 30 * MINUTE, owner=ownership(pid_start_token="")
        )
        verdict = evaluate_receipt_freshness(
            legacy,
            chain,
            now_ms=T0 + 30 * MINUTE,
            run_timeout_seconds=DEEP_RESEARCH_SECONDS,
        )
        self.assertEqual(STATE_STALE, verdict.state)
        self.assertEqual(("browser_receipt_unrefreshable",), verdict.reasons)
        self.assertEqual(0, verdict.refresh_count)

    def test_a_receipt_without_a_start_token_still_works_while_young(self) -> None:
        legacy = receipt(pid_start_token="")
        verdict = evaluate_receipt_freshness(legacy, now_ms=T0 + 10 * MINUTE)
        self.assertEqual(STATE_FRESH, verdict.state)
        self.assertTrue(verdict.usable)

    def test_a_recycled_pid_is_caught_by_the_start_token(self) -> None:
        # Same pid and port, different process: exactly the case an age-based
        # check was standing in for.
        recycled = ownership(pid_start_token="boot-9f2a:998877")
        chain = [OwnershipObservation(observed_at_ms=T0 + MINUTE, ownership=recycled)]
        verdict = evaluate_receipt_freshness(receipt(), chain, now_ms=T0 + 2 * MINUTE)
        self.assertEqual(STATE_BROKEN, verdict.state)
        self.assertEqual(("browser_identity_changed",), verdict.reasons)

    def test_ownership_reports_whether_it_pins_process_identity(self) -> None:
        self.assertTrue(ownership().pins_process_identity)
        self.assertFalse(ownership(pid_start_token="").pins_process_identity)


class InputValidationTests(ReceiptTestCase):
    """Malformed input refuses; it never silently becomes a verdict."""

    def test_receipt_fields_are_validated(self) -> None:
        for override in (
            {"pid": 0},
            {"pid": True},
            {"pid": "4242"},
            {"port": 80},
            {"port": 70000},
            {"target_id": "short"},
            {"target_id": "G" * 32},
            {"profile_fingerprint": "0" * 63},
            {"profile_fingerprint": "Z" * 64},
            {"pid_start_token": "has space"},
        ):
            self.assert_refused(
                "receipt_invalid", lambda override=override: ownership(**override)
            )

    def test_a_non_receipt_is_refused(self) -> None:
        for value in (None, {}, "receipt", ownership()):
            self.assert_refused(
                "receipt_invalid",
                lambda value=value: evaluate_receipt_freshness(value, now_ms=T0),
            )

    def test_now_and_window_arguments_are_validated(self) -> None:
        self.assert_refused(
            "doctor_input_invalid",
            lambda: evaluate_receipt_freshness(receipt(), now_ms="now"),
        )
        self.assert_refused(
            "doctor_input_invalid",
            lambda: evaluate_receipt_freshness(receipt(), now_ms=T0, max_age_ms=0),
        )
        self.assert_refused(
            "doctor_input_invalid",
            lambda: evaluate_receipt_freshness(
                receipt(), now_ms=T0, refresh_interval_ms=-1
            ),
        )
        self.assert_refused(
            "doctor_input_invalid",
            lambda: evaluate_receipt_freshness(
                receipt(), now_ms=T0, max_lifetime_ms=RECEIPT_MAX_LIFETIME_MS + 1
            ),
        )

    def test_a_receipt_from_the_future_is_refused(self) -> None:
        self.assert_refused(
            "doctor_input_invalid",
            lambda: evaluate_receipt_freshness(
                receipt(T0 + FUTURE_SKEW_MS + 1), now_ms=T0
            ),
        )
        # Inside the tolerated skew it is accepted, matching the JS doctor.
        verdict = evaluate_receipt_freshness(receipt(T0 + FUTURE_SKEW_MS), now_ms=T0)
        self.assertTrue(verdict.usable)

    def test_observation_containers_are_validated(self) -> None:
        for value in ("chain", b"chain", {"observed_at_ms": T0}):
            self.assert_refused(
                "observation_invalid",
                lambda value=value: evaluate_receipt_freshness(
                    receipt(), value, now_ms=T0
                ),
            )
        self.assert_refused(
            "observation_invalid",
            lambda: evaluate_receipt_freshness(receipt(), [object()], now_ms=T0),
        )

    def test_observation_fields_are_validated(self) -> None:
        self.assert_refused(
            "observation_invalid",
            lambda: OwnershipObservation(observed_at_ms=-1, ownership=ownership()),
        )
        self.assert_refused(
            "observation_invalid",
            lambda: OwnershipObservation(observed_at_ms=T0, ownership=None),
        )
        self.assert_refused(
            "observation_invalid",
            lambda: OwnershipObservation(
                observed_at_ms=T0, ownership=ownership(), verified="yes"
            ),
        )

    def test_ownership_from_mapping_rejects_unknown_fields(self) -> None:
        payload = {
            "pid": 4242,
            "port": 9222,
            "target_id": "A1B2C3D4E5F60718",
            "profile_fingerprint": "0" * 64,
        }
        self.assertEqual(
            ownership(pid_start_token=""), BrowserOwnership.from_mapping(payload)
        )
        self.assert_refused(
            "receipt_invalid",
            lambda: BrowserOwnership.from_mapping({**payload, "cookies": "x"}),
        )
        self.assert_refused(
            "receipt_invalid", lambda: BrowserOwnership.from_mapping(None)
        )


class ContractTests(ReceiptTestCase):
    """Invariants that keep this contract aligned with its neighbours."""

    def test_every_refusal_code_in_the_source_is_declared(self) -> None:
        source = RECEIPT_SOURCE.read_text(encoding="utf-8")
        used = set(re.findall(r'_refuse\("([a-z_]+)"\)', source))
        self.assertTrue(used)
        self.assertEqual(set(), used - REFUSAL_CODES)

    def test_the_lifetime_ceiling_covers_the_policy_ceiling(self) -> None:
        from runtime_manager import oracle_policy

        bounds = oracle_policy._INTEGER_BOUNDS["max_runtime_seconds"]
        self.assertEqual(bounds[1], MAX_RUN_SECONDS)
        self.assertGreaterEqual(RECEIPT_MAX_LIFETIME_MS, MAX_RUN_SECONDS * 1000)

    def test_the_refresh_interval_leaves_slack_before_the_window_closes(self) -> None:
        # A refresher that misses one beat must still have time to recover.
        self.assertLess(RECEIPT_REFRESH_INTERVAL_MS, RECEIPT_MAX_AGE_MS)

    def test_refusals_share_the_oracle_error_surface(self) -> None:
        error = self.assert_refused(
            "receipt_invalid", lambda: evaluate_receipt_freshness(None, now_ms=T0)
        )
        self.assertIsInstance(error, OracleBrokerError)
        payload = error.to_payload()
        self.assertFalse(payload["ok"])
        self.assertEqual("receipt_invalid", payload["error"]["code"])

    def test_contract_payload_is_json_shaped_and_complete(self) -> None:
        payload = contract_payload()
        self.assertEqual(ORACLE_RECEIPT_FRESHNESS_SCHEMA, payload["schema"])
        self.assertEqual(RECEIPT_MAX_AGE_MS, payload["receipt_max_age_ms"])
        self.assertEqual(MAX_RUN_SECONDS, payload["max_run_seconds"])
        self.assertEqual(list(TRANSPORT_CHECK_KEYS), payload["transport_checks"])

    def test_the_verdict_payload_carries_no_host_detail(self) -> None:
        verdict = evaluate_receipt_freshness(receipt(), now_ms=T0 + MINUTE)
        payload = verdict.to_payload()
        rendered = repr(payload)
        for leak in ("4242", "9222", "A1B2C3D4E5F60718"):
            self.assertNotIn(leak, rendered)

    def test_the_js_doctor_still_agrees_on_the_base_window(self) -> None:
        # Drift detector across the language boundary: the JS doctor owns the
        # live check, and its base ceiling must stay the one this module
        # documents. Skips where the skills checkout is absent.
        for candidate in JS_DOCTOR_CANDIDATES:
            if candidate.is_file():
                source = candidate.read_text(encoding="utf-8")
                break
        else:
            self.skipTest("skills checkout with oracle-subagent-auth.mjs not present")
        match = re.search(
            r"const RECEIPT_MAX_AGE_MS\s*=\s*([0-9_\s*]+);", source
        )
        self.assertIsNotNone(match, "RECEIPT_MAX_AGE_MS not found in the JS doctor")
        product = 1
        for factor in match.group(1).split("*"):
            product *= int(factor.strip().replace("_", ""))
        self.assertEqual(RECEIPT_MAX_AGE_MS, product)


if __name__ == "__main__":
    unittest.main()
