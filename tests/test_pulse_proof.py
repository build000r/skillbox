from __future__ import annotations

import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
PERF_DIR = ROOT_DIR / "tests" / "perf"
if str(PERF_DIR) not in sys.path:
    sys.path.insert(0, str(PERF_DIR))

PROOF = SourceFileLoader(
    "skillbox_pulse_proof_module",
    str((PERF_DIR / "pulse_proof.py").resolve()),
).load_module()


class PulseProofTests(unittest.TestCase):
    """Regression guard: the no-infra pulse proof must reproduce a stable
    behavioral fingerprint and assert the heal/recover/pressure invariants."""

    @classmethod
    def setUpClass(cls) -> None:
        # Small cycle count keeps the timing pass fast in CI.
        cls.proof = PROOF.build_proof(cycles=20)

    def test_behavioral_fingerprint_is_deterministic(self) -> None:
        again = PROOF.build_proof(cycles=5)
        self.assertEqual(
            self.proof["behavioral_fingerprint_sha256"],
            again["behavioral_fingerprint_sha256"],
            "pulse behavioral proof is not reproducible — a regression changed pulse behavior",
        )

    def test_service_crash_auto_heals(self) -> None:
        svc = self.proof["behavioral"]["service_transitions"]
        self.assertEqual(svc["final_service_states"], {"web": "running"})
        self.assertGreaterEqual(svc["heals"], 1)

    def test_check_fail_then_recovers(self) -> None:
        chk = self.proof["behavioral"]["check_transitions"]
        self.assertEqual(chk["final_check_states"], {"disk": True})
        self.assertGreaterEqual(chk["events_emitted"], 2)

    def test_pressure_advisory_is_read_only_and_clears(self) -> None:
        pressure = self.proof["behavioral"]["pressure_advisory"]
        self.assertFalse(pressure["advisory_mutates"])
        self.assertEqual(pressure["final_pressure_warnings"], [])
        self.assertGreaterEqual(pressure["events_emitted"], 2)

    def test_state_file_shape_has_core_keys(self) -> None:
        keys = set(self.proof["behavioral"]["state_file_shape"]["top_level_keys"])
        for required in ("service_states", "check_states", "cycle_count", "heals", "events_emitted"):
            self.assertIn(required, keys)

    def test_status_renders_without_error(self) -> None:
        status = self.proof["behavioral"]["status_rendering"]
        self.assertEqual(status["exit_code"], 0)
        joined = "\n".join(status["stdout_normalized"])
        self.assertIn("pulse:", joined)
        self.assertIn("cycles:", joined)

    def test_timing_baseline_is_recorded(self) -> None:
        timing = self.proof["timing"]
        self.assertGreater(timing["cycles"], 0)
        self.assertGreater(timing["avg_ms"], 0.0)
        self.assertGreaterEqual(timing["max_ms"], timing["p50_ms"])

    def test_blocked_conditions_are_explicit(self) -> None:
        self.assertTrue(self.proof["blocked_conditions"])


if __name__ == "__main__":
    unittest.main()
