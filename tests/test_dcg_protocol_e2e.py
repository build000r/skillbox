"""DCG multi-agent protocol e2e proof (skillbox-dcg-agent-protocol-e2e-ln4z).

Two halves, and both matter:

1. **The proof runs for real.** The harness invokes the pinned `dcg` binary
   through the actual generated Claude/Codex/Grok hook documents in a disposable
   home. The verdict boundary is the real binary, not a patched function -- a
   mocked boundary would happily "prove" whatever the mock was told to say.
2. **The receipt is not self-certifying.** Every planted failure below must be
   REJECTED by the validator. A validator that only ever passes is decoration.

Safety: the harness never executes a payload. It hands the string to the hook
and reads the verdict. A sentinel path is asserted absent as the independent
check on that claim.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

HARNESS = ROOT_DIR / "scripts" / "dcg-protocol-e2e.py"
VALIDATOR = ROOT_DIR / "scripts" / "validate-dcg-receipt.py"
FIXTURES = ROOT_DIR / "tests" / "fixtures" / "dcg_protocol"

DCG_BIN = os.environ.get("SKILLBOX_DCG_BIN") or shutil.which("dcg")
REQUIRES_BINARY = unittest.skipIf(
    not DCG_BIN,
    "pinned dcg binary unavailable; this proof is meaningless against a stand-in",
)


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False, **kwargs)


def _implementation_sha() -> str:
    return _run(["git", "-C", str(ROOT_DIR), "rev-parse", "HEAD"]).stdout.strip()


class FixtureContractTests(unittest.TestCase):
    """The fixtures must describe the real protocol, hermetically."""

    def setUp(self) -> None:
        self.protocol = json.loads((FIXTURES / "protocol.json").read_text(encoding="utf-8"))

    def test_all_three_agents_are_covered(self) -> None:
        self.assertEqual(
            ["claude", "codex", "grok"],
            sorted(spec["name"] for spec in self.protocol["agents"]),
        )

    def test_fixture_matcher_matches_the_shipped_reconciler(self) -> None:
        from runtime_manager import dcg_reconcile as R

        self.assertEqual(R.HOOK_MATCHER, self.protocol["matcher"])
        self.assertEqual(R.HOOK_EVENT, self.protocol["hook_event"])

    def test_every_referenced_payload_exists(self) -> None:
        names = [self.protocol["malformed_payload"]]
        for spec in self.protocol["agents"]:
            names += [spec["safe"], spec["destructive"]]
        for probe in self.protocol["limitation_probes"]:
            names.append(probe["payload"])
        for name in names:
            with self.subTest(payload=name):
                self.assertTrue((FIXTURES / "payloads" / name).is_file(), name)

    def test_destructive_payload_is_actually_destructive_shaped(self) -> None:
        """If the payload were harmless, 'deny' would prove nothing."""
        payload = json.loads(
            (FIXTURES / "payloads" / "claude-destructive.json").read_text(encoding="utf-8")
        )
        command = payload["tool_input"]["command"]
        self.assertIn("rm", command)
        self.assertIn("-rf", command)

    def test_malformed_payload_is_genuinely_malformed(self) -> None:
        raw = (FIXTURES / "payloads" / "malformed.json").read_text(encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            json.loads(raw)


@REQUIRES_BINARY
class ProtocolE2ETests(unittest.TestCase):
    """The real proof: real binary, real generated hooks, disposable home."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.receipt_path = root / "receipt.json"
        cls.sentinel = root / "EXECUTED_SENTINEL"
        cls.result = _run(
            [
                sys.executable, str(HARNESS),
                "--fixture-root", str(FIXTURES),
                "--home", str(root / "home"),
                "--output", str(cls.receipt_path),
                "--sentinel", str(cls.sentinel),
            ]
        )
        cls.receipt = (
            json.loads(cls.receipt_path.read_text(encoding="utf-8"))
            if cls.receipt_path.is_file()
            else {}
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_harness_succeeds_and_prints_the_marker(self) -> None:
        self.assertEqual(0, self.result.returncode, self.result.stderr)
        self.assertIn("DCG_PROTOCOL_E2E_OK", self.result.stdout)

    def test_every_agent_allows_the_harmless_command(self) -> None:
        for entry in self.receipt["agents"]:
            with self.subTest(agent=entry["name"]):
                self.assertEqual("allow", entry["safe"]["decision"])

    def test_every_agent_denies_the_destructive_command(self) -> None:
        for entry in self.receipt["agents"]:
            with self.subTest(agent=entry["name"]):
                self.assertEqual("deny", entry["destructive"]["decision"])

    def test_the_verdict_came_from_the_real_pinned_binary(self) -> None:
        """Guards the bead's stop condition: a mocked boundary proves nothing."""
        self.assertTrue(self.receipt["binary_version"].startswith("0.6.7"))
        self.assertEqual(64, len(self.receipt["binary_sha256"]))
        for entry in self.receipt["agents"]:
            self.assertTrue(entry["hook_command"], "hook command must be a real command")

    def test_hook_command_is_read_from_the_generated_document(self) -> None:
        for entry in self.receipt["agents"]:
            with self.subTest(agent=entry["name"]):
                self.assertIn("dcg", entry["hook_command"][0])

    def test_deny_carries_a_reason_from_the_binary(self) -> None:
        for entry in self.receipt["agents"]:
            with self.subTest(agent=entry["name"]):
                self.assertTrue(entry["destructive"].get("reason"), "deny must explain itself")

    def test_malformed_input_fails_closed(self) -> None:
        self.assertNotEqual("allow", self.receipt["malformed"]["decision"])

    def test_nothing_executed_and_the_sentinel_is_absent(self) -> None:
        self.assertIs(False, self.receipt["executed"])
        for entry in self.receipt["agents"]:
            self.assertIs(False, entry["executed"])
        self.assertFalse(self.sentinel.exists())
        self.assertFalse(self.receipt["sentinel"]["present"])

    def test_receipt_carries_every_required_marker(self) -> None:
        for marker in (
            "implementation_sha", "binary_version", "binary_sha256",
            "policy_sha256", "hook_state_sha256", "timestamp",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.receipt)
                self.assertTrue(str(self.receipt[marker]).strip())
        for entry in self.receipt["agents"]:
            for marker in ("agent", "timestamp"):
                self.assertIn(marker, entry)
            for probe in ("safe", "destructive"):
                self.assertIn("payload_sha256", entry[probe])
                self.assertIn("decision", entry[probe])
                self.assertIn("executed", entry[probe])

    # --- limitations, surfaced rather than hidden ---------------------------

    def test_codex_unified_exec_limitation_is_surfaced_with_evidence(self) -> None:
        """The bead asks for this to be SURFACED, and it is a real gap.

        DCG's verdict keys on tool_name == "Bash". A Codex unified_exec payload
        carries a different tool name, so the guard does not apply to it. The
        receipt records the observed decision rather than quietly omitting it.
        """
        probe = next(
            item for item in self.receipt["limitations"] if item["name"] == "codex-unified-exec"
        )
        self.assertFalse(probe["guarded"], "unified_exec is genuinely not guarded")
        self.assertEqual("allow", probe["observed_decision"])
        self.assertTrue(probe["why"])

    def test_codex_trust_limitation_is_surfaced(self) -> None:
        probe = next(item for item in self.receipt["limitations"] if item["name"] == "codex-trust")
        self.assertIn("trust_state", probe)
        self.assertTrue(probe["why"])

    def test_limitations_are_not_counted_as_guarded_coverage(self) -> None:
        for probe in self.receipt["limitations"]:
            with self.subTest(limitation=probe["name"]):
                self.assertIn("guarded", probe)


@REQUIRES_BINARY
class ReceiptValidatorTests(unittest.TestCase):
    """Planted failures. A validator that never rejects is decoration."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.receipt_path = root / "receipt.json"
        _run(
            [
                sys.executable, str(HARNESS),
                "--fixture-root", str(FIXTURES),
                "--home", str(root / "home"),
                "--output", str(cls.receipt_path),
            ]
        )
        cls.good = json.loads(cls.receipt_path.read_text(encoding="utf-8"))
        # The harness does not yet emit `worktree_clean` (that field belongs to
        # scripts/dcg-protocol-e2e.py, a different owner). The validator now
        # REQUIRES it — absent is a rejection, proven by
        # test_probe_absent_worktree_clean_fails below — so the "good receipt"
        # baseline stamps it explicitly rather than relying on the old, weaker
        # behaviour (skillbox-jkl3).
        cls.good["worktree_clean"] = True
        cls.sha = _implementation_sha()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _validate(
        self, receipt: dict, *, sha: str | None = None, extra: list[str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Write the receipt into the CANONICAL evidence layout, then validate.

        The old helper wrote to a random temp filename, which is exactly the
        weakness skillbox-jkl3 fixed: the validator could not bind a receipt to
        its location. The directory is derived from the receipt's own
        implementation_sha so path identity is exercised for real.
        """
        directory = Path(tempfile.mkdtemp()) / "evidence" / "dcg" / str(
            receipt.get("implementation_sha") or "0" * 40
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "receipt.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        argv = [sys.executable, str(VALIDATOR), str(path)]
        if sha is not None:
            argv += ["--implementation-sha", sha]
        argv += extra or []
        return _run(argv)

    def test_a_good_receipt_passes(self) -> None:
        result = self._validate(self.good, sha=self.sha)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("DCG_RECEIPT_OK", result.stdout)

    def test_probe_missing_identity_field_fails(self) -> None:
        bad = json.loads(json.dumps(self.good))
        del bad["binary_sha256"]
        result = self._validate(bad, sha=self.sha)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("missing required field: binary_sha256", result.stderr)

    def test_probe_empty_identity_field_fails(self) -> None:
        bad = json.loads(json.dumps(self.good))
        bad["binary_version"] = "   "
        result = self._validate(bad, sha=self.sha)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("empty", result.stderr)

    def test_probe_stale_source_sha_fails(self) -> None:
        bad = json.loads(json.dumps(self.good))
        bad["implementation_sha"] = "0" * 40
        result = self._validate(bad, sha=self.sha)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("stale source SHA", result.stderr)

    def test_probe_absent_binary_digest_fails(self) -> None:
        for field in ("binary_sha256", "policy_sha256", "hook_state_sha256"):
            with self.subTest(field=field):
                bad = json.loads(json.dumps(self.good))
                bad[field] = "absent"
                result = self._validate(bad, sha=self.sha)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("absent", result.stderr)

    def test_probe_malformed_payload_allowed_fails(self) -> None:
        bad = json.loads(json.dumps(self.good))
        bad["malformed"]["decision"] = "allow"
        result = self._validate(bad, sha=self.sha)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("must fail closed", result.stderr)

    def test_probe_timeout_is_not_a_verdict(self) -> None:
        bad = json.loads(json.dumps(self.good))
        bad["agents"][0]["destructive"]["timed_out"] = True
        result = self._validate(bad, sha=self.sha)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("timed out", result.stderr)

    def test_probe_destructive_allowed_fails(self) -> None:
        bad = json.loads(json.dumps(self.good))
        bad["agents"][0]["destructive"]["decision"] = "allow"
        result = self._validate(bad, sha=self.sha)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("expected 'deny'", result.stderr)

    def test_probe_executed_true_fails(self) -> None:
        bad = json.loads(json.dumps(self.good))
        bad["agents"][0]["executed"] = True
        result = self._validate(bad, sha=self.sha)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("executed must be false", result.stderr)

    def test_probe_present_execution_sentinel_fails(self) -> None:
        bad = json.loads(json.dumps(self.good))
        bad["sentinel"]["present"] = True
        result = self._validate(bad, sha=self.sha)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("guarded command RAN", result.stderr)

    def test_probe_missing_agent_fails(self) -> None:
        bad = json.loads(json.dumps(self.good))
        bad["agents"] = [a for a in bad["agents"] if a["name"] != "grok"]
        result = self._validate(bad, sha=self.sha)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("does not cover required agent: grok", result.stderr)

    # --- identity binding (skillbox-jkl3) -----------------------------------

    def test_probe_wrong_path_segment_fails(self) -> None:
        """The bead's first acceptance probe: right SHA, wrong directory."""
        directory = Path(tempfile.mkdtemp()) / "evidence" / "dcg" / ("0" * 40)
        directory.mkdir(parents=True)
        path = directory / "receipt.json"
        path.write_text(json.dumps(self.good), encoding="utf-8")
        result = _run(
            [sys.executable, str(VALIDATOR), str(path), "--implementation-sha", self.sha]
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("path/receipt SHA mismatch", result.stderr)

    def test_probe_unparseable_path_segment_fails(self) -> None:
        """A directory that is not a SHA cannot bind identity at all."""
        directory = Path(tempfile.mkdtemp()) / "not-a-sha"
        directory.mkdir(parents=True)
        path = directory / "receipt.json"
        path.write_text(json.dumps(self.good), encoding="utf-8")
        result = _run(
            [sys.executable, str(VALIDATOR), str(path), "--implementation-sha", self.sha]
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("not a 40-hex implementation SHA", result.stderr)

    def test_probe_dirty_tree_receipt_fails(self) -> None:
        """The bead's second acceptance probe."""
        bad = json.loads(json.dumps(self.good))
        bad["worktree_clean"] = False
        result = self._validate(bad, sha=self.sha)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("dirty", result.stderr)

    def test_probe_absent_worktree_clean_fails(self) -> None:
        """Unverifiable is never the same as verified."""
        bad = json.loads(json.dumps(self.good))
        del bad["worktree_clean"]
        result = self._validate(bad, sha=self.sha)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("does not declare worktree_clean", result.stderr)

    def test_downgrade_flags_warn_loudly_and_mark_not_identity_bound(self) -> None:
        """A downgrade is allowed, but never silent."""
        bad = json.loads(json.dumps(self.good))
        del bad["worktree_clean"]
        result = self._validate(bad, sha=self.sha, extra=["--allow-dirty-tree"])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("DCG_RECEIPT_WARN", result.stderr)
        self.assertIn("NOT identity-bound", result.stdout)

    def test_allow_dirty_tree_cannot_silence_require_clean_tree(self) -> None:
        """Two flags that contradict each other must not cancel out."""
        result = self._validate(
            self.good, sha=self.sha, extra=["--allow-dirty-tree", "--require-clean-tree"]
        )
        # This repo's worktree is dirty during the wave, so the live check bites.
        self.assertNotEqual(0, result.returncode)
        self.assertIn("--require-clean-tree", result.stderr)

    def test_validator_rejects_a_non_receipt(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("not json")
            path = handle.name
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        result = _run([sys.executable, str(VALIDATOR), path])
        self.assertNotEqual(0, result.returncode)
        self.assertIn("not valid JSON", result.stderr)

    def test_validator_rejects_a_missing_receipt(self) -> None:
        result = _run([sys.executable, str(VALIDATOR), "/nonexistent/receipt.json"])
        self.assertNotEqual(0, result.returncode)


@REQUIRES_BINARY
class HarnessFaultTests(unittest.TestCase):
    """Absent artifacts must fail the harness, not be quietly tolerated."""

    def test_absent_binary_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = _run(
                [
                    sys.executable, str(HARNESS),
                    "--fixture-root", str(FIXTURES),
                    "--home", str(root / "home"),
                    "--output", str(root / "receipt.json"),
                    "--binary", str(root / "no-such-dcg"),
                ]
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("DCG_PROTOCOL_E2E_FAIL", result.stderr)

    def test_preexisting_sentinel_fails_closed(self) -> None:
        """A stale sentinel would make 'absent afterwards' meaningless."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sentinel = root / "EXECUTED_SENTINEL"
            sentinel.write_text("stale\n", encoding="utf-8")
            result = _run(
                [
                    sys.executable, str(HARNESS),
                    "--fixture-root", str(FIXTURES),
                    "--home", str(root / "home"),
                    "--output", str(root / "receipt.json"),
                    "--sentinel", str(sentinel),
                ]
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("sentinel already present", result.stderr)

    def test_harness_writes_nothing_outside_home_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "guard").mkdir()
            witness = root / "guard" / "untouched.txt"
            witness.write_text("keep\n", encoding="utf-8")
            _run(
                [
                    sys.executable, str(HARNESS),
                    "--fixture-root", str(FIXTURES),
                    "--home", str(root / "home"),
                    "--output", str(root / "receipt.json"),
                ]
            )
            self.assertEqual("keep\n", witness.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
