"""Contract tests for the canonical Skillbox DCG policy.

Covers the six things that make ``.dcg.toml`` a policy instead of a string:
defaults, overlay order, dedupe, invalid inputs, the audited fail-open
exception, deterministic re-render, and upstream (dcg) syntax validation.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import date
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import dcg_policy as DP  # noqa: E402
from runtime_manager.errors import ValidationError  # noqa: E402

FIXTURES = ROOT_DIR / "tests" / "fixtures" / "dcg_policy"
GOLDEN_CASES = ("default", "overlay_order", "dedupe", "audited_exception")

# .dcg.toml is a generated, gitignored artifact: present on a converged box,
# absent on a fresh checkout. Assert on it only when it exists.
REPO_DCG_TOML = ROOT_DIR / ".dcg.toml"

DCG_BIN = shutil.which("dcg")


def _overlays(case: str) -> list[dict[str, object]]:
    return json.loads((FIXTURES / f"{case}.overlays.json").read_text(encoding="utf-8"))


def _expected(case: str) -> str:
    return (FIXTURES / f"{case}.expected.toml").read_text(encoding="utf-8")


def _run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "runtime_manager.dcg_policy", *args],
        cwd=str(cwd or ROOT_DIR),
        env={"PYTHONPATH": str(ENV_MANAGER_DIR), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


class DefaultPolicyTests(unittest.TestCase):
    def test_default_policy_is_core_git_plus_filesystem_and_fail_closed(self) -> None:
        policy = DP.build_policy()
        self.assertEqual(policy.packs, ("core.git", "core.filesystem"))
        self.assertTrue(policy.fail_closed)
        self.assertEqual(policy.allowlist, ())
        self.assertEqual(policy.blocklist, DP.DEFAULT_BLOCK_RULES)
        self.assertIsNone(policy.exception)

    def test_default_render_has_ntm_redirect_without_a_permissive_allowlist(self) -> None:
        text = DP.render()
        self.assertIn("[overrides]", text)
        self.assertNotIn("allow =", text)
        self.assertIn("block =", text)
        self.assertIn("vibing-with-ntm", text)
        self.assertIn("--robot-wait", text)

    def test_render_preserves_the_generated_ownership_marker(self) -> None:
        text = DP.render()
        self.assertTrue(text.startswith(DP.GENERATED_MARKER))
        self.assertIn(DP.POLICY_MARKER, text)

    def test_fail_closed_marker_is_emitted_at_column_zero(self) -> None:
        # The lifecycle/doctor probes grep for this exact anchored line.
        self.assertIn("\nfail_closed = true\n", DP.render())

    def test_approved_packs_contain_the_defaults(self) -> None:
        self.assertTrue(set(DP.DEFAULT_PACKS).issubset(DP.APPROVED_PACKS))


class GoldenRenderTests(unittest.TestCase):
    def test_every_golden_case_renders_byte_identically(self) -> None:
        for case in GOLDEN_CASES:
            with self.subTest(case=case):
                self.assertEqual(DP.render(_overlays(case)), _expected(case))

    def test_overlay_order_is_defaults_first_then_overlay_declaration_order(self) -> None:
        policy = DP.build_policy(_overlays("overlay_order"))
        self.assertEqual(
            policy.packs,
            (
                "core.git",
                "core.filesystem",
                "containers.docker",
                "database.postgresql",
                "kubernetes.kubectl",
                "containers.compose",
            ),
        )
        self.assertEqual(
            policy.allowlist,
            (
                "docker compose down --volumes acme-dev",
                "kubectl delete namespace globex-ephemeral",
            ),
        )

    def test_dedupe_is_first_wins_across_and_within_overlays(self) -> None:
        policy = DP.build_policy(_overlays("dedupe"))
        self.assertEqual(
            policy.packs,
            ("core.git", "core.filesystem", "containers.docker", "database.postgresql"),
        )
        self.assertEqual(policy.allowlist, ("docker compose down --volumes acme-dev",))

    def test_overlays_cannot_drop_the_default_packs(self) -> None:
        # There is no removal verb; every case still carries the floor.
        for case in GOLDEN_CASES:
            with self.subTest(case=case):
                policy = DP.build_policy(_overlays(case))
                for pack in DP.DEFAULT_PACKS:
                    self.assertIn(pack, policy.packs)

    def test_rerender_is_deterministic(self) -> None:
        for case in GOLDEN_CASES:
            with self.subTest(case=case):
                overlays = _overlays(case)
                self.assertEqual(DP.render(overlays), DP.render(overlays))


class AuditedExceptionTests(unittest.TestCase):
    def test_audited_exception_disables_fail_closed_and_records_the_audit(self) -> None:
        policy = DP.build_policy(_overlays("audited_exception"))
        self.assertFalse(policy.fail_closed)
        assert policy.exception is not None
        self.assertEqual(policy.exception.ticket, "SEC-4417")
        self.assertEqual(policy.exception.approved_by, "skillbox-security-lead")
        text = DP.render_policy(policy)
        self.assertIn("\nfail_closed = false\n", text)
        for field in DP.EXCEPTION_FIELDS:
            self.assertIn(f"#   {field}: ", text)

    def test_a_later_overlay_can_restore_fail_closed(self) -> None:
        overlays = _overlays("audited_exception") + [{"id": "hardening", "fail_closed": True}]
        policy = DP.build_policy(overlays)
        self.assertTrue(policy.fail_closed)
        self.assertIsNone(policy.exception)

    def test_expired_exception_is_rejected_at_render_time(self) -> None:
        overlays = _overlays("audited_exception")
        with self.assertRaises(ValidationError) as ctx:
            DP.build_policy(overlays, now=date(2099, 6, 1))
        self.assertEqual(ctx.exception.code, DP.DCG_POLICY_EXPIRED_EXCEPTION)


class InvalidInputTests(unittest.TestCase):
    def test_every_invalid_fixture_raises_its_declared_code(self) -> None:
        cases = json.loads((FIXTURES / "invalid.json").read_text(encoding="utf-8"))
        self.assertTrue(cases)
        for case in cases:
            with self.subTest(case=case["name"]):
                with self.assertRaises(ValidationError) as ctx:
                    DP.build_policy(case["overlays"])
                self.assertEqual(ctx.exception.code, case["code"])
                self.assertIn(ctx.exception.code, DP.DCG_POLICY_ERROR_CODES)

    def test_allowlist_ceiling_rejects_a_de_facto_disablement(self) -> None:
        rules = [f"cargo test --package pkg{index}" for index in range(DP.MAX_ALLOWLIST_RULES + 1)]
        with self.assertRaises(ValidationError) as ctx:
            DP.build_policy([{"id": "acme", "allowlist": rules}])
        self.assertEqual(ctx.exception.code, DP.DCG_POLICY_BROAD_ALLOWLIST)

    def test_overlay_must_be_a_mapping(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            DP.build_policy([["core.git"]])
        self.assertEqual(ctx.exception.code, DP.DCG_POLICY_MALFORMED_OVERLAY)


class UpstreamValidationTests(unittest.TestCase):
    def test_rendered_documents_only_use_pinned_upstream_keys(self) -> None:
        for case in GOLDEN_CASES:
            with self.subTest(case=case):
                document = tomllib.loads(_expected(case))
                self.assertTrue(DP.key_paths(document).issubset(DP.UPSTREAM_KEY_PATHS))

    def test_validate_rendered_round_trips_every_golden(self) -> None:
        for case in GOLDEN_CASES:
            with self.subTest(case=case):
                text = _expected(case)
                policy = DP.validate_rendered(text)
                self.assertEqual(DP.render_policy(policy), text)

    def test_legacy_allowlist_rules_block_is_rejected_as_upstream_mismatch(self) -> None:
        # The pre-policy renderer emitted `[allowlist] rules = [...]`, a table
        # dcg never reads. Validation must call that out, not pass it through.
        text = DP.GENERATED_MARKER + '\n\n[allowlist]\nrules = ["rm -rf build"]\n'
        with self.assertRaises(ValidationError) as ctx:
            DP.validate_rendered(text)
        self.assertEqual(ctx.exception.code, DP.DCG_POLICY_UPSTREAM_MISMATCH)

    def test_missing_ownership_marker_is_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            DP.validate_rendered("[general]\nfail_closed = true\n")
        self.assertEqual(ctx.exception.code, DP.DCG_POLICY_UPSTREAM_MISMATCH)

    @unittest.skipUnless(REPO_DCG_TOML.exists(), ".dcg.toml is generated + gitignored")
    def test_repo_dcg_toml_matches_the_default_render(self) -> None:
        self.assertEqual(REPO_DCG_TOML.read_text(encoding="utf-8"), DP.render())

    @unittest.skipIf(DCG_BIN is None, "dcg binary not installed")
    def test_approved_packs_all_exist_upstream(self) -> None:
        result = subprocess.run(
            [str(DCG_BIN), "packs", "--format", "json"],
            capture_output=True,
            text=True,
            check=True,
        )
        upstream = {pack["id"] for pack in json.loads(result.stdout)["packs"]}
        self.assertEqual(sorted(DP.APPROVED_PACKS - upstream), [])

    @unittest.skipIf(DCG_BIN is None, "dcg binary not installed")
    def test_rendered_default_policy_denies_a_destructive_git_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "dcg.toml"
            config.write_text(DP.render(), encoding="utf-8")
            result = subprocess.run(
                [str(DCG_BIN), "test", "--config", str(config), "--format", "json",
                 "git reset --hard"],
                capture_output=True,
                text=True,
                check=False,
            )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["decision"], "deny")
        self.assertEqual(payload["pack_id"], "core.git")


class CliTests(unittest.TestCase):
    def test_render_to_output_is_byte_identical_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            one = Path(tmp) / "one.toml"
            two = Path(tmp) / "two.toml"
            for target in (one, two):
                result = _run_cli("render", "--output", str(target), "--format", "json")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["fail_closed"], True)
            self.assertEqual(one.read_bytes(), two.read_bytes())
            self.assertEqual(one.read_text(encoding="utf-8"), _expected("default"))

    def test_render_accepts_format_before_the_subcommand(self) -> None:
        result = _run_cli("--format", "json", "render")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["content"], DP.render())

    def test_render_applies_overlay_files_in_order(self) -> None:
        result = _run_cli(
            "render",
            "--overlay",
            str(FIXTURES / "overlay_order.overlays.json"),
            "--format",
            "json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["content"], _expected("overlay_order"))

    def test_failure_probes_exit_nonzero_with_a_typed_code(self) -> None:
        probes = {
            "unknown_pack": DP.DCG_POLICY_UNKNOWN_PACK,
            "broad_allowlist_wildcard": DP.DCG_POLICY_BROAD_ALLOWLIST,
            "malformed_allowlist_type": DP.DCG_POLICY_MALFORMED_ALLOWLIST,
            "unaudited_fail_open": DP.DCG_POLICY_UNAUDITED_FAIL_OPEN,
        }
        cases = {
            case["name"]: case
            for case in json.loads((FIXTURES / "invalid.json").read_text(encoding="utf-8"))
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, code in probes.items():
                with self.subTest(probe=name):
                    overlay_path = Path(tmp) / f"{name}.json"
                    overlay_path.write_text(json.dumps(cases[name]["overlays"]), encoding="utf-8")
                    result = _run_cli(
                        "render", "--overlay", str(overlay_path), "--format", "json"
                    )
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertEqual(json.loads(result.stdout)["error"]["code"], code)

    def test_validate_accepts_a_rendered_policy_and_rejects_a_legacy_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rendered = Path(tmp) / "rendered.toml"
            rendered.write_text(DP.render(), encoding="utf-8")
            ok = _run_cli("validate", "--config", str(rendered), "--format", "json")
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertTrue(json.loads(ok.stdout)["fail_closed"])

            legacy = Path(tmp) / "legacy.toml"
            legacy.write_text(
                DP.GENERATED_MARKER + "\n\n[packs]\nenabled = ['core.git']\n",
                encoding="utf-8",
            )
            bad = _run_cli("validate", "--config", str(legacy), "--format", "json")
            self.assertEqual(bad.returncode, 1, bad.stdout)
            self.assertEqual(
                json.loads(bad.stdout)["error"]["code"], DP.DCG_POLICY_UPSTREAM_MISMATCH
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
