"""Fail-closed contract tests for the ``dcg`` doctor check.

The check this replaces was ``path_exists``: green the moment a file existed at
``$SKILLBOX_DCG_BIN``. So the property under test is not "does the healthy case
report healthy" — it is **"can any broken state reach healthy"**. The matrix in
:class:`BrokenStateMatrixTests` is therefore the point of the module, and every
row asserts three things at once:

1. the DCG-native verdict is not ``healthy``
2. the doctor family status is ``fail`` (never ``warn``, never ``inco``, never
   skipped — an advisory DCG verdict is the exact false green this bead removes)
3. exactly ONE remediation command is printed

Every fixture is a disposable temp home materialized from
``tests/fixtures/dcg_reconcile``; nothing here reads or writes the real
``$HOME``, and the doctor is read-only so no test needs to undo it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
SCRIPTS_DIR = ROOT_DIR / "scripts"
for _path in (ENV_MANAGER_DIR, SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from runtime_manager import dcg_distribution as DD  # noqa: E402
from runtime_manager import dcg_doctor as DOC  # noqa: E402
from runtime_manager import dcg_reconcile as DR  # noqa: E402

RECONCILE_FIXTURES = ROOT_DIR / "tests" / "fixtures" / "dcg_reconcile"
BIN_TOKEN = "@DCG_BIN@"

#: A compose file that mounts everything the reconciler writes. The real
#: docker-compose.yml does too; this keeps the matrix independent of it so a
#: change there cannot silently turn a persistence row green.
GOOD_COMPOSE = "\n".join(
    f"      - ${{SKILLBOX_STATE_ROOT}}/home/{subtree}:/home/sandbox/{subtree}"
    for subtree in DOC.PERSISTED_SUBTREES
)


class _DoctorCase(unittest.TestCase):
    """Materializes a converged, trusted home plus a matching fake repo root."""

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="dcg-doctor-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.tmp = tmp
        self.home = tmp / "home"
        shutil.copytree(RECONCILE_FIXTURES / "container_home" / "home", self.home, symlinks=True)
        self.binary = self.home / DR.DEFAULT_BINARY_RELPATH
        for path in sorted(self.home.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if BIN_TOKEN in text:
                path.write_text(text.replace(BIN_TOKEN, str(self.binary)), encoding="utf-8")
        self.binary.chmod(0o755)

        # A repo root whose compose file mounts every persisted subtree.
        self.root = tmp / "repo"
        self.root.mkdir()
        (self.root / "docker-compose.yml").write_text(
            f"services:\n  workspace:\n    volumes:\n{GOOD_COMPOSE}\n", encoding="utf-8"
        )

    # -- the converged baseline -------------------------------------------

    def converge(self) -> None:
        DR.apply(self.home, binary=self.binary)

    def trust(self, value: str = "c" * 64) -> None:
        """Persist the trust hash Codex itself would write after its modal."""
        config = self.home / DR.CODEX_CONFIG_RELPATH
        config.parent.mkdir(parents=True, exist_ok=True)
        base = config.read_text(encoding="utf-8") if config.is_file() else 'model = "gpt-5.6-sol"\n'
        base = base.split("[hooks.state.")[0].rstrip("\n")
        config.write_text(
            base + f'\n\n[hooks.state."user:PreToolUse:0"]\nenabled = true\ntrusted_hash = "{value}"\n',
            encoding="utf-8",
        )

    def healthy(self) -> None:
        self.converge()
        self.trust()

    def model(self, *, mcp_command: str = "mcp-server") -> dict:
        return {
            "artifacts": [
                {
                    "id": DD.ARTIFACT_ID,
                    "host_path": str(self.binary),
                    "sync": {"mode": "manual"},
                }
            ],
            "services": [
                {
                    "id": "dcg-mcp",
                    "artifact": DD.ARTIFACT_ID,
                    "command": f"{self.binary} {mcp_command}",
                    "healthcheck": {
                        "type": "mcp_ready",
                        "probe_command": f"{self.binary} {mcp_command}",
                    },
                }
            ],
        }

    def report(self, **kwargs) -> dict:
        return DOC.collect(self.model(**kwargs), self.root)


# ---------------------------------------------------------------------------
# The healthy baseline
# ---------------------------------------------------------------------------


class HealthyStateTests(_DoctorCase):
    def test_a_fully_converged_trusted_home_is_healthy(self) -> None:
        self.healthy()
        report = self.report()
        self.assertEqual(report["failures"], [], report["message"])
        self.assertEqual(report["dcg_status"], DOC.STATUS_HEALTHY)

    def test_the_healthy_report_carries_every_asserted_field(self) -> None:
        # These are exactly the fields the bead's acceptance jq reads.
        self.healthy()
        report = self.report()
        self.assertEqual(report["id"], "dcg")
        self.assertEqual(report["binary"]["version"], DD.DCG_VERSION)
        self.assertEqual(report["binary"]["version"], "v0.6.7")
        self.assertIs(report["policy"]["fail_closed"], True)
        self.assertEqual(report["hooks"]["claude"], "healthy")
        self.assertEqual(report["hooks"]["codex"], "trusted")
        self.assertEqual(report["hooks"]["grok"], "healthy")
        self.assertEqual(report["mcp"]["command"], "mcp-server")

    def test_the_family_finding_passes_and_keeps_the_family_vocabulary(self) -> None:
        self.healthy()
        result = DOC.check_result(self.model(), self.root)
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.code, "dcg")
        self.assertEqual(result.extra["dcg_status"], DOC.STATUS_HEALTHY)
        # `extra` promotes domain fields but must never redefine the family's.
        self.assertNotIn("status", result.extra)
        self.assertNotIn("code", result.extra)

    def test_known_interception_gaps_are_printed_even_when_healthy(self) -> None:
        # "healthy" must not read as "nothing can run unguarded".
        self.healthy()
        report = self.report()
        surfaces = {item["surface"] for item in report["limitations"]}
        self.assertIn("direct-shell", surfaces)
        self.assertIn("codex-unified-exec", surfaces)

    def test_the_doctor_mutates_nothing(self) -> None:
        # Non-goal of this bead: mutation inside doctor. Prove it by digesting
        # the whole home before and after.
        self.healthy()

        def digest() -> dict[str, bytes]:
            return {
                str(path.relative_to(self.home)): path.read_bytes()
                for path in sorted(self.home.rglob("*"))
                if path.is_file() and not path.is_symlink()
            }

        before = digest()
        self.report()
        DOC.check_result(self.model(), self.root)
        self.assertEqual(digest(), before)


# ---------------------------------------------------------------------------
# The broken-state matrix — the actual point of the module
# ---------------------------------------------------------------------------


class BrokenStateMatrixTests(_DoctorCase):
    """Every damaged state must fail. Presence must never imply protection."""

    # -- damage functions, one per row ------------------------------------

    def damage_absent_binary(self) -> str:
        self.binary.unlink()
        return DOC.DCG_DOCTOR_BINARY_ABSENT

    def damage_non_executable_binary(self) -> str:
        self.binary.chmod(0o644)
        return DOC.DCG_DOCTOR_BINARY_NOT_EXECUTABLE

    def damage_wrong_version(self) -> str:
        self.binary.write_text("#!/bin/sh\necho \"0.5.0\"\n", encoding="utf-8")
        self.binary.chmod(0o755)
        return DOC.DCG_DOCTOR_BINARY_VERSION_MISMATCH

    def damage_unreadable_version(self) -> str:
        # A binary that exists and runs but will not say what it is: unverified
        # provenance is not protection.
        self.binary.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        self.binary.chmod(0o755)
        return DOC.DCG_DOCTOR_BINARY_UNVERIFIED

    def damage_policy_absent(self) -> str:
        (self.home / DR.POLICY_RELPATH).unlink()
        return DOC.DCG_DOCTOR_POLICY_ABSENT

    def damage_policy_malformed(self) -> str:
        (self.home / DR.POLICY_RELPATH).write_text("this is not = valid = toml\n", encoding="utf-8")
        return DOC.DCG_DOCTOR_POLICY_MALFORMED

    def damage_policy_fail_open(self) -> str:
        policy = self.home / DR.POLICY_RELPATH
        text = policy.read_text(encoding="utf-8").replace(
            "fail_closed = true", "fail_closed = false"
        )
        policy.write_text(text, encoding="utf-8")
        return DOC.DCG_DOCTOR_POLICY_FAIL_OPEN

    def damage_missing_claude_hook(self) -> str:
        (self.home / DR.CLAUDE_SETTINGS_RELPATH).write_text("{}\n", encoding="utf-8")
        return DOC.DCG_DOCTOR_HOOK_UNHEALTHY

    def damage_missing_grok_hook(self) -> str:
        (self.home / DR.GROK_HOOK_RELPATH).unlink()
        return DOC.DCG_DOCTOR_HOOK_UNHEALTHY

    def damage_wrong_path_hook(self) -> str:
        # The hook still exists but points at a binary that is not the managed
        # one: a hook aimed at nothing guards nothing.
        settings = self.home / DR.CLAUDE_SETTINGS_RELPATH
        text = settings.read_text(encoding="utf-8").replace(
            str(self.binary), "/nonexistent/elsewhere/dcg"
        )
        settings.write_text(text, encoding="utf-8")
        return DOC.DCG_DOCTOR_HOOK_UNHEALTHY

    def damage_absent_codex_trust(self) -> str:
        config = self.home / DR.CODEX_CONFIG_RELPATH
        config.write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
        return DOC.DCG_DOCTOR_CODEX_TRUST_ABSENT

    def damage_stale_codex_trust(self) -> str:
        # Codex trusted an OLDER hook; the hook has since been rewritten, so the
        # persisted hash no longer matches what Codex would be asked to run.
        # `self.binary` deliberately stays at the managed path: the model still
        # declares the same artifact, only the hook bytes moved on.
        moved = self.home / ".local" / "bin" / "dcg-next"
        shutil.copy2(self.binary, moved)
        DR.apply(self.home, binary=moved)
        # Converge back, so the hook is once again a correct hook at the managed
        # path -- and still one Codex has never seen. This is the dangerous
        # shape: everything looks right, and Codex will refuse to run it.
        DR.apply(self.home, binary=self.binary)
        return DOC.DCG_DOCTOR_CODEX_TRUST_STALE

    # -- the table --------------------------------------------------------

    DAMAGE = (
        "absent_binary",
        "non_executable_binary",
        "wrong_version",
        "unreadable_version",
        "policy_absent",
        "policy_malformed",
        "policy_fail_open",
        "missing_claude_hook",
        "missing_grok_hook",
        "wrong_path_hook",
        "absent_codex_trust",
        "stale_codex_trust",
    )

    def test_every_broken_state_fails_closed(self) -> None:
        for name in self.DAMAGE:
            with self.subTest(damage=name):
                self.setUp()
                self.healthy()
                expected_reason = getattr(self, f"damage_{name}")()
                report = self.report()

                self.assertNotEqual(
                    report["dcg_status"],
                    DOC.STATUS_HEALTHY,
                    f"{name} reached healthy: presence is being read as protection",
                )
                self.assertIn(expected_reason, report["failures"])

                result = DOC.check_result(self.model(), self.root)
                self.assertEqual(
                    result.status,
                    "fail",
                    f"{name} produced {result.status!r}; a required DCG failure is never advisory",
                )
                self.assertTrue(result.fix_command)

    def test_each_broken_state_prints_exactly_one_remediation(self) -> None:
        for name in self.DAMAGE:
            with self.subTest(damage=name):
                self.setUp()
                self.healthy()
                getattr(self, f"damage_{name}")()
                report = self.report()
                remediation = report["remediation"]
                self.assertIsInstance(remediation, str)
                self.assertTrue(remediation.strip())
                self.assertNotIn("\n", remediation, "more than one remediation command")

    def test_the_remediation_ladder_installs_before_it_asks_for_trust(self) -> None:
        # A host with no binary must not be told to go trust a hook that does
        # not exist yet.
        self.healthy()
        self.damage_absent_binary()
        self.damage_absent_codex_trust()
        report = self.report()
        self.assertIn(DOC.DCG_DOCTOR_BINARY_ABSENT, report["failures"])
        self.assertIn(DOC.DCG_DOCTOR_CODEX_TRUST_ABSENT, report["failures"])
        self.assertIn("install_verified_binary", report["remediation"])

    def test_an_untrusted_codex_hook_is_needs_operator_action_not_healthy(self) -> None:
        self.healthy()
        self.damage_absent_codex_trust()
        report = self.report()
        self.assertEqual(report["dcg_status"], DOC.STATUS_NEEDS_OPERATOR)
        self.assertNotEqual(report["dcg_status"], DOC.STATUS_HEALTHY)
        self.assertEqual(report["hooks"]["codex"], DR.CODEX_TRUST_ABSENT)
        # Still a hard doctor failure: an untrusted hook guards as much as a
        # missing one.
        self.assertEqual(DOC.check_result(self.model(), self.root).status, "fail")

    def test_the_bypass_flag_never_satisfies_the_trust_gate(self) -> None:
        # CODEX_HOOK_TRUST_REQUIRED: the escape hatch is not a remediation. The
        # flag DOES appear in the remediation text -- as a prohibition -- so the
        # assertion is that it is forbidden there, not merely absent.
        self.healthy()
        self.damage_absent_codex_trust()
        report = self.report()
        remediation = report["remediation"]
        self.assertIn(f"Never pass {DR.BYPASS_FLAG}", remediation)
        # And it is never offered as something to run.
        self.assertNotIn(f"dcg {DR.BYPASS_FLAG}", remediation)
        self.assertNotIn(f"--yes {DR.BYPASS_FLAG}", remediation)

    # -- rows that live outside the home ----------------------------------

    def test_a_missing_persistent_mount_fails(self) -> None:
        self.healthy()
        (self.root / "docker-compose.yml").write_text(
            "services:\n  workspace:\n    volumes:\n"
            "      - ${SKILLBOX_STATE_ROOT}/home/.claude:/home/sandbox/.claude\n",
            encoding="utf-8",
        )
        report = self.report()
        self.assertIn(DOC.DCG_DOCTOR_PERSISTENCE_MISSING, report["failures"])
        self.assertIn(".config/dcg", report["persistence"]["missing"])

    def test_the_obsolete_mcp_spelling_fails(self) -> None:
        # `dcg mcp` was removed in 0.6.7; a bridge still declaring it is dead.
        self.healthy()
        report = self.report(mcp_command=DD.DCG_OBSOLETE_MCP_COMMAND)
        self.assertIn(DOC.DCG_DOCTOR_MCP_OBSOLETE_COMMAND, report["failures"])
        self.assertNotEqual(report["dcg_status"], DOC.STATUS_HEALTHY)

    def test_a_fail_open_operator_adapter_fails(self) -> None:
        self.healthy()
        from lib import dcglib

        # The regression bead scpz removed: an adapter that treats "no verdict"
        # as permission.
        with mock.patch.object(dcglib, "dcg_blocks_execution", return_value=False):
            report = self.report()
        self.assertIn(DOC.DCG_DOCTOR_ADAPTER_FAIL_OPEN, report["failures"])
        self.assertNotEqual(report["dcg_status"], DOC.STATUS_HEALTHY)

    def test_the_supported_adapter_protocol_is_part_of_health(self) -> None:
        self.healthy()
        report = self.report()
        self.assertEqual(report["adapter"]["interface"], "dcg test --robot --format json")
        self.assertIs(report["adapter"]["fail_closed"], True)

    def test_a_runtime_declaring_no_dcg_binary_fails_rather_than_skipping(self) -> None:
        report = DOC.collect({"artifacts": [], "services": []}, self.root)
        self.assertEqual(report["dcg_status"], DOC.STATUS_FAILED)
        self.assertIn(DOC.DCG_DOCTOR_NOT_DECLARED, report["failures"])


# ---------------------------------------------------------------------------
# Wiring into the real doctor surface
# ---------------------------------------------------------------------------


class DoctorSurfaceTests(unittest.TestCase):
    def test_manage_doctor_emits_the_dcg_check_with_promoted_fields(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(ENV_MANAGER_DIR / "manage.py"),
                "doctor",
                "--profile",
                "core",
                "--format",
                "json",
            ],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            timeout=600,
        )
        # 0 or 4: a doctor that RAN reports, it does not crash.
        self.assertIn(proc.returncode, (0, 4), proc.stderr[-2000:])
        payload = json.loads(proc.stdout)
        checks = [check for check in payload["checks"] if check.get("id") == "dcg"]
        self.assertEqual(len(checks), 1, "the dcg check is missing from manage.py doctor")
        check = checks[0]

        # The promoted fields the acceptance contract reads.
        for key in ("binary", "policy", "hooks", "mcp", "dcg_status"):
            self.assertIn(key, check)
        self.assertEqual(check["mcp"]["command"], "mcp-server")
        self.assertEqual(check["binary"]["expected_version"], "v0.6.7")

        # The family vocabulary is untouched: `extra` promotes domain fields, it
        # does not get to redefine `status`.
        self.assertIn(check["status"], ("pass", "warn", "inco", "fail"))
        self.assertTrue(check["fix_command"])

    def test_a_failing_dcg_check_makes_the_doctor_exit_nonzero(self) -> None:
        from runtime_manager import runtime_ops
        from runtime_manager._shared.errors import CheckResult

        broken = CheckResult(
            status="fail", code="dcg", message="broken", details={}, fix_command="x"
        )
        with mock.patch.object(runtime_ops, "dcg_doctor_results", return_value=[broken]):
            from lib import doctor_contract

            findings = [doctor_contract.finding_from_obj(broken)]
            self.assertEqual(
                doctor_contract.exit_code_for(findings), doctor_contract.EXIT_DRIFT
            )

    def test_the_check_is_skipped_only_when_no_dcg_binary_is_declared(self) -> None:
        from runtime_manager import runtime_ops

        self.assertEqual(
            runtime_ops.dcg_doctor_results({"artifacts": []}, ROOT_DIR), []
        )
        results = runtime_ops.dcg_doctor_results(
            {"artifacts": [{"id": DD.ARTIFACT_ID, "host_path": "/nope/.local/bin/dcg"}]},
            ROOT_DIR,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].code, "dcg")


class RuntimeManifestTests(unittest.TestCase):
    def test_the_dcg_binary_check_is_declared_required(self) -> None:
        # The "required flip": an agent box whose guard is absent is not a
        # working box, and `required: false` is how a host with no protection
        # at all reported "required runtime checks passed".
        text = (ROOT_DIR / "workspace" / "runtime.yaml").read_text(encoding="utf-8")
        block = text.split("- id: dcg-binary", 1)[1].split("- id: ", 1)[0]
        self.assertIn("required: true", block)
        self.assertNotIn("required: false", block)


if __name__ == "__main__":
    unittest.main()
