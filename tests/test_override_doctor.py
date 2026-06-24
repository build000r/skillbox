"""Doctor/lint coverage for repo-local skill override policies."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager.policy_eval import lint_repo_override_policy  # noqa: E402
from runtime_manager import skill_visibility as SKILL_VISIBILITY  # noqa: E402
from runtime_manager.structure_doctor import (  # noqa: E402
    STATUS_FAIL,
    DoctorContext,
    _run_repo_skill_override_lint,
)
from runtime_manager.validation import validate_repo_skill_override_policy  # noqa: E402


def _make_repo(root: Path, text: str) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    policy_dir = repo / ".skillbox"
    policy_dir.mkdir()
    (policy_dir / "skill-overrides.yaml").write_text(text, encoding="utf-8")
    return repo


def _find(payload: dict[str, object], rule: str, skill: str | None = None) -> dict[str, object]:
    for finding in payload.get("findings") or []:
        if finding.get("rule") == rule and (skill is None or finding.get("skill") == skill):
            return finding
    raise AssertionError(f"missing finding rule={rule!r} skill={skill!r}: {payload}")


class RepoSkillOverrideLintTests(unittest.TestCase):
    def test_clean_override_file_yields_zero_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(
                Path(tmpdir),
                "version: 1\npin_on: [alpha]\npin_off: [beta]\n",
            )

            payload = lint_repo_override_policy(repo, known_skill_names=["alpha", "beta"])

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["findings"], [])
        self.assertTrue(payload["exists"])

    def test_contradiction_finding_names_both_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(
                Path(tmpdir),
                "version: 1\npin_on:\n  - alpha\npin_off:\n  - alpha\n",
            )

            payload = lint_repo_override_policy(repo, known_skill_names=["alpha"])

        finding = _find(payload, "contradiction", "alpha")
        self.assertEqual(finding["severity"], "error")
        self.assertEqual(finding["lines"], {"pin_on": 3, "pin_off": 5})
        self.assertIn("pin_on", str(finding["explanation"]))
        self.assertIn("pin_off", str(finding["explanation"]))

    def test_floor_opt_out_of_dispatcher_core_is_hard_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(
                Path(tmpdir),
                "version: 1\nopt_out_global:\n  - smart\n  - sbp\n",
            )

            payload = lint_repo_override_policy(repo, known_skill_names=["smart", "sbp"])

        smart = _find(payload, "floor_opt_out", "smart")
        sbp = _find(payload, "floor_opt_out", "sbp")
        self.assertEqual(smart["severity"], "error")
        self.assertEqual(sbp["severity"], "error")
        self.assertEqual(smart["code"], "OVERRIDE_REFUSED_FLOOR")
        self.assertFalse(payload["ok"])

    def test_dangling_pin_has_did_you_mean_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir), "version: 1\npin_on: [smrt]\n")

            payload = lint_repo_override_policy(repo, known_skill_names=["smart"])

        finding = _find(payload, "dangling", "smrt")
        self.assertEqual(finding["severity"], "error")
        self.assertEqual(finding["did_you_mean"], "smart")
        self.assertIn("smart", str(finding["suggested_fix"]))

    def test_dangling_check_uses_normal_skill_source_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir), "version: 1\npin_on: [alpha]\n")
            source_root = Path(tmpdir) / "sources"
            source_root.mkdir()

            with (
                mock.patch.object(
                    SKILL_VISIBILITY,
                    "_declared_skill_occurrences",
                    return_value=([{"name": "beta"}], []),
                ),
                mock.patch.object(
                    SKILL_VISIBILITY,
                    "_skill_source_roots",
                    return_value=[source_root],
                ),
                mock.patch.object(
                    SKILL_VISIBILITY,
                    "_skill_source_candidates",
                    return_value=[{"name": "alpha"}],
                ),
            ):
                results = validate_repo_skill_override_policy({}, cwd=repo)

        self.assertEqual(results[0].status, "pass")
        self.assertEqual(results[0].details["findings"], [])

    def test_parse_error_is_a_nonfatal_lint_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir), "version: 1\npin_on: [unterminated\n")

            payload = lint_repo_override_policy(repo, known_skill_names=["alpha"])

        finding = _find(payload, "parse_error")
        self.assertEqual(finding["severity"], "error")
        self.assertIn("parse", str(finding["explanation"]).lower())
        self.assertFalse(payload["ok"])

    def test_non_mapping_yaml_is_a_nonfatal_lint_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir), "- alpha\n")

            payload = lint_repo_override_policy(repo, known_skill_names=["alpha"])

        finding = _find(payload, "parse_error")
        self.assertEqual(finding["severity"], "error")
        self.assertTrue(
            "mapping" in str(finding["explanation"]) or "YAML object" in str(finding["explanation"])
        )

    def test_validation_wrapper_returns_failed_check_for_floor_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir), "version: 1\nopt_out_global: [smart]\n")

            results = validate_repo_skill_override_policy({}, cwd=repo)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "fail")
        self.assertEqual(results[0].code, "repo-skill-override-lint")
        self.assertEqual(results[0].details["findings"][0]["rule"], "floor_opt_out")

    def test_structure_doctor_gate_reports_override_lint_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = _make_repo(Path(tmpdir), "version: 1\nopt_out_global: [sbp]\n")
            ctx = DoctorContext(
                runtime_root=ROOT_DIR,
                config_root=None,
                cwd=repo,
                _model={},
            )

            status, detail = _run_repo_skill_override_lint(ctx)

        self.assertEqual(status, STATUS_FAIL)
        self.assertIn("repo skill override", detail)


if __name__ == "__main__":
    unittest.main()
