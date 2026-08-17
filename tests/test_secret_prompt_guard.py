"""Credential-shaped literals must never reach a command line.

Every sample here is SYNTHETIC. The strings are shaped like credentials and are
not credentials — that distinction is the whole reason this file can exist in a
public tree at all, and the reason the guard fingerprints matches instead of
echoing them.

The tests are organised around the two ways this guard could fail: it could miss
a real shape (the burn happens again), or it could leak the value it caught into
the very transcript it exists to keep clean.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT_DIR / "scripts" / "secret-prompt-guard"
PACK_PATH = ROOT_DIR / "scripts" / "dcg-packs" / "secrets-prompt-guard.yaml"

_spec = importlib.util.spec_from_loader(
    "secret_prompt_guard",
    importlib.machinery.SourceFileLoader("secret_prompt_guard", str(GUARD_PATH)),
)
assert _spec and _spec.loader
GUARD = importlib.util.module_from_spec(_spec)
# Register before exec: @dataclass resolves annotations through
# sys.modules[cls.__module__], which is not yet populated during exec_module.
sys.modules["secret_prompt_guard"] = GUARD
_spec.loader.exec_module(GUARD)

# Synthetic shapes: correct prefix, obviously fake body.
TSKEY = "tskey-auth-kFAKEFAKE0-FAKEFAKEFAKEFAKEFAKEFAKE"
SK_KEY = "sk-FAKEFAKEFAKEFAKEFAKEFAKE"
AKIA_KEY = "AKIAFAKEFAKEFAKE1234"
GHP_KEY = "ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE"
BEARER = "Bearer FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFA"
ALL_SHAPES = (TSKEY, SK_KEY, AKIA_KEY, GHP_KEY, BEARER)


class GuardTestCase(unittest.TestCase):
    def run_guard(self, *argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        environment.pop(GUARD.OVERRIDE_ENV, None)
        if env:
            environment.update(env)
        return subprocess.run(
            [sys.executable, str(GUARD_PATH), *argv],
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
        )


class DetectionTests(GuardTestCase):
    """Every enumerated shape is caught."""

    def test_each_shape_is_detected(self) -> None:
        for sample in ALL_SHAPES:
            findings = GUARD.scan_value(f"please use {sample} to join")
            self.assertEqual(1, len(findings), sample)

    def test_the_burn_case_is_detected_in_a_realistic_prompt(self) -> None:
        # The literal shape of the 2026-07-30 incident.
        findings = GUARD.scan_argv(
            ["amp", "-x", f"enroll the orb with {TSKEY} and report back"]
        )
        self.assertEqual(1, len(findings))
        self.assertEqual("tailscale_auth_key", findings[0].shape)
        self.assertEqual(2, findings[0].argv_index)

    def test_multiple_shapes_in_one_argument_are_all_reported(self) -> None:
        findings = GUARD.scan_value(f"{TSKEY} and {AKIA_KEY}")
        self.assertEqual(
            {"tailscale_auth_key", "aws_access_key_id"},
            {item.shape for item in findings},
        )

    def test_the_shape_table_covers_the_bead_vocabulary(self) -> None:
        names = {shape.name for shape in GUARD.SHAPES}
        self.assertEqual(
            {
                "tailscale_auth_key",
                "openai_style_key",
                "aws_access_key_id",
                "github_pat",
                "bearer_token",
            },
            names,
        )


class FalsePositiveTests(GuardTestCase):
    """A guard that blocks ordinary prompts gets disabled, so precision matters."""

    def test_ordinary_prompts_pass(self) -> None:
        for clean in (
            "summarize the tailnet posture contract",
            "rotate the orb auth key and tell me when done",
            "the tskey was burned on 2026-07-30",  # talking ABOUT it is fine
            "run make doctor and report failures",
            "git log --oneline -20",
        ):
            self.assertEqual([], GUARD.scan_value(clean), clean)

    def test_near_misses_do_not_trip(self) -> None:
        for near in (
            "sk-short",  # below the length floor
            "AKIA123",  # too short for an access key id
            "Bearer token",  # the word, not a token
            "tskey-auth-",  # prefix with no body
            "ghp_short",
            "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",  # a git sha
        ):
            self.assertEqual([], GUARD.scan_value(near), near)

    def test_a_secret_name_is_not_a_secret_value(self) -> None:
        # The remediation this guard recommends must not itself be blocked.
        self.assertEqual([], GUARD.scan_value("amp secrets set --user TS_AUTHKEY"))
        self.assertEqual([], GUARD.scan_value("use $TS_AUTHKEY from the environment"))


class NoLeakTests(GuardTestCase):
    """The guard must never copy the value into the transcript it protects."""

    def test_the_blocked_message_never_contains_the_value(self) -> None:
        result = self.run_guard("--", "amp", "-x", f"join with {TSKEY}")
        self.assertEqual(GUARD.EXIT_BLOCKED, result.returncode)
        combined = result.stdout + result.stderr
        self.assertNotIn(TSKEY, combined)
        # The distinctive body must not appear even in fragments.
        self.assertNotIn("kFAKEFAKE0", combined)
        self.assertIn("tailscale_auth_key", combined)

    def test_the_json_report_never_contains_the_value(self) -> None:
        result = self.run_guard(
            "--check-only", "--format", "json", "--", "amp", "-x", TSKEY
        )
        self.assertNotIn(TSKEY, result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["blocked"])
        self.assertEqual(1, len(payload["findings"]))
        self.assertNotIn(TSKEY, json.dumps(payload))

    def test_the_fingerprint_identifies_without_revealing(self) -> None:
        first = GUARD.fingerprint(TSKEY)
        self.assertEqual(first, GUARD.fingerprint(TSKEY), "stable across calls")
        self.assertNotEqual(first, GUARD.fingerprint(SK_KEY))
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        self.assertNotIn(first, TSKEY)

    def test_the_report_fingerprints_the_match_not_the_whole_prompt(self) -> None:
        # Two different prompts carrying the same key must fingerprint alike, or
        # an operator cannot tell it is the same key that keeps leaking.
        one = GUARD.scan_value(f"alpha {TSKEY} beta")[0]
        two = GUARD.scan_value(f"completely different text {TSKEY}")[0]
        self.assertEqual(one.fingerprint, two.fingerprint)


class ExecBehaviourTests(GuardTestCase):
    """Block means the command never runs; clean means it runs unchanged."""

    def test_a_clean_command_is_exec_ed_transparently(self) -> None:
        result = self.run_guard("--", "echo", "hello from the wrapped command")
        self.assertEqual(0, result.returncode)
        self.assertIn("hello from the wrapped command", result.stdout)

    def test_the_wrapped_command_keeps_its_own_exit_code(self) -> None:
        result = self.run_guard("--", sys.executable, "-c", "import sys; sys.exit(7)")
        self.assertEqual(7, result.returncode)

    def test_a_blocked_command_never_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "ran"
            result = self.run_guard("--", "touch", str(marker), SK_KEY)
            self.assertEqual(GUARD.EXIT_BLOCKED, result.returncode)
            self.assertFalse(marker.exists(), "the guard let the command run")

    def test_check_only_never_execs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "ran"
            result = self.run_guard("--check-only", "--", "touch", str(marker))
            self.assertEqual(0, result.returncode)
            self.assertFalse(marker.exists())

    def test_no_command_is_a_usage_error(self) -> None:
        self.assertEqual(GUARD.EXIT_USAGE, self.run_guard().returncode)

    def test_the_blocked_exit_code_is_distinguishable(self) -> None:
        # Not 1 and not 2, so a caller can tell "the guard refused" from "the
        # wrapped command failed" or "you used the guard wrong".
        self.assertEqual(3, GUARD.EXIT_BLOCKED)
        self.assertNotIn(GUARD.EXIT_BLOCKED, {0, 1, GUARD.EXIT_USAGE})


class OverrideTests(GuardTestCase):
    """Warn-and-block with a named override; silent pass is never available."""

    def test_the_override_downgrades_a_block_to_a_warning(self) -> None:
        result = self.run_guard(
            "--", "echo", f"ok {AKIA_KEY}", env={GUARD.OVERRIDE_ENV: "1"}
        )
        self.assertEqual(0, result.returncode)
        self.assertIn("WARNING (override active)", result.stderr)
        self.assertIn("ok ", result.stdout, "the command still ran")

    def test_the_override_still_reports_the_finding(self) -> None:
        # An override must be visible in the transcript, never silent.
        result = self.run_guard(
            "--", "echo", f"ok {AKIA_KEY}", env={GUARD.OVERRIDE_ENV: "1"}
        )
        self.assertIn("aws_access_key_id", result.stderr)

    def test_only_explicit_truthy_values_override(self) -> None:
        for value in ("0", "", "no", "maybe"):
            self.assertFalse(GUARD.override_active({GUARD.OVERRIDE_ENV: value}), value)
        for value in ("1", "true", "YES"):
            self.assertTrue(GUARD.override_active({GUARD.OVERRIDE_ENV: value}), value)

    def test_the_override_name_is_in_the_remediation_text(self) -> None:
        self.assertIn(GUARD.OVERRIDE_ENV, GUARD.REMEDIATION)


class RemediationTests(GuardTestCase):
    """The guard has to say what to do instead, or it just annoys people."""

    def test_the_remediation_names_the_correct_delivery_path(self) -> None:
        self.assertIn("amp secrets set --user", GUARD.REMEDIATION)
        self.assertIn("--data-file -", GUARD.REMEDIATION)
        self.assertIn("docs/orb-tailnet-bootstrap.md", GUARD.REMEDIATION)

    def test_the_remediation_points_at_sections_that_exist(self) -> None:
        # A guard citing a heading nobody wrote sends the operator nowhere.
        doc = (ROOT_DIR / "docs" / "orb-tailnet-bootstrap.md").read_text(
            encoding="utf-8"
        )
        for heading in (
            "Transitional auth-key delivery",
            "3. Prompt embedding is forbidden",
        ):
            self.assertIn(heading, GUARD.REMEDIATION)
            self.assertIn(f"# {heading}", doc)

    def test_the_remediation_is_printed_on_a_block(self) -> None:
        result = self.run_guard("--", "amp", "-x", TSKEY)
        self.assertIn("amp secrets set --user", result.stderr)


class DcgPackParityTests(GuardTestCase):
    """The DCG pack and the wrapper must agree; two halves, one vocabulary."""

    def pack(self) -> dict:
        try:
            import yaml
        except ModuleNotFoundError:  # pragma: no cover - PyYAML is optional here
            self.skipTest("PyYAML not installed")
        return yaml.safe_load(PACK_PATH.read_text(encoding="utf-8"))

    def test_the_pack_exists_and_declares_the_expected_identity(self) -> None:
        pack = self.pack()
        self.assertEqual("secrets.prompt_guard", pack["id"])
        self.assertRegex(pack["version"], r"^\d+\.\d+\.\d+$")

    def test_the_pack_covers_every_shape_the_wrapper_knows(self) -> None:
        # Drift here means one surface blocks what the other allows.
        pack = self.pack()
        pack_patterns = {row["pattern"] for row in pack["destructive_patterns"]}
        wrapper_patterns = {shape.pattern.pattern for shape in GUARD.SHAPES}
        self.assertEqual(wrapper_patterns, pack_patterns)

    def test_every_pack_pattern_matches_its_synthetic_sample(self) -> None:
        pack = self.pack()
        for row in pack["destructive_patterns"]:
            compiled = re.compile(row["pattern"])
            self.assertTrue(
                any(compiled.search(sample) for sample in ALL_SHAPES),
                row["name"],
            )

    def test_the_pack_carries_no_real_looking_secret(self) -> None:
        text = PACK_PATH.read_text(encoding="utf-8")
        # The pack states shapes as regexes; it must not contain a literal that
        # its own patterns would match.
        for shape in GUARD.SHAPES:
            for match in shape.pattern.finditer(text):
                self.assertIn("FAKE", match.group(0), match.group(0))

    def test_the_pack_records_that_it_is_not_yet_loaded(self) -> None:
        # It cannot self-activate: [packs].custom_paths lives in generated
        # .dcg.toml. Saying so beats a pack that looks live and is not.
        text = PACK_PATH.read_text(encoding="utf-8")
        self.assertIn("NOT YET LOADED", text)
        self.assertIn("custom_paths", text)


class SourceHygieneTests(GuardTestCase):
    """No real secret may enter this tree, including via these files."""

    def test_neither_the_guard_nor_its_tests_carry_a_non_synthetic_shape(self) -> None:
        for path in (GUARD_PATH, Path(__file__)):
            text = path.read_text(encoding="utf-8")
            for shape in GUARD.SHAPES:
                for match in shape.pattern.finditer(text):
                    self.assertIn(
                        "FAKE", match.group(0), f"{path.name}: {match.group(0)[:12]}…"
                    )


if __name__ == "__main__":
    unittest.main()
