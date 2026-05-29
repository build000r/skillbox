from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import evidence as EVID  # noqa: E402
from runtime_manager.evidence import (  # noqa: E402
    EVIDENCE_TOP_LEVEL_KEYS,
    collect_runtime_evidence,
    runtime_evidence_markdown,
    print_runtime_evidence_text,
)
from runtime_manager.shared import CheckResult  # noqa: E402


def _status_payload(*, pressure_warnings=None, services=None):
    return {
        "services": services if services is not None else [{"id": "api", "state": "running"}],
        "tasks": [],
        "repos": [],
        "blocked_services": [],
        "active_profiles": ["core"],
        "active_clients": [],
        "pressure_advisory": {
            "ok": not pressure_warnings,
            "warnings": list(pressure_warnings or []),
            "mode": "read_only",
            "mutates": False,
            "local_disk": {"free_gib": 100.0, "pressure_level": "normal"},
        },
    }


def _skills_payload(*, effective=10, issues=0, shadowed=0):
    summary = {
        "effective": effective,
        "shadowed": shadowed,
        "broken_global": issues,
        "broken_project": 0,
        "global_not_allowed": 0,
        "extra_global": 0,
    }
    return {"summary": summary, "next_actions": []}


def _mcp_payload(*, unexplained_drift=0, invalid_configs=0):
    return {
        "summary": {"expected": 2, "unexplained_drift": unexplained_drift, "invalid_configs": invalid_configs},
        "parity": {"claude_only": [], "claude_only_declared": []},
        "next_actions": [],
    }


class RuntimeEvidenceTests(unittest.TestCase):
    def _collect(
        self,
        root: Path,
        *,
        doctor,
        status,
        skills,
        mcp,
        dirty,
        write_pulse_state=True,
    ):
        if write_pulse_state:
            state_dir = root / "logs" / "runtime"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "pulse.state.json").write_text(
                json.dumps({"updated_at": time.time(), "cycle_count": 5, "heals": 1, "events_emitted": 3})
                + "\n",
                encoding="utf-8",
            )
        branch = mock.Mock(returncode=0, stdout="main\n")
        with (
            mock.patch.object(EVID, "doctor_results", return_value=doctor),
            mock.patch.object(EVID, "runtime_status", return_value=status),
            mock.patch.object(EVID, "collect_skill_visibility", return_value=skills),
            mock.patch.object(EVID, "collect_mcp_audit", return_value=mcp),
            mock.patch.object(EVID, "git_dirty_paths", return_value=dirty),
            mock.patch.object(EVID, "run_command", return_value=branch),
        ):
            return collect_runtime_evidence(root, {"services": []}, cwd=str(root))

    def test_green_state_has_stable_keys_and_no_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = self._collect(
                root,
                doctor=[CheckResult("pass", "a", "ok"), CheckResult("pass", "b", "ok")],
                status=_status_payload(),
                skills=_skills_payload(),
                mcp=_mcp_payload(),
                dirty=[],
            )
        self.assertEqual(sorted(payload.keys()), sorted(EVIDENCE_TOP_LEVEL_KEYS))
        self.assertEqual(
            sorted(payload["sections"].keys()),
            ["beads", "doctor", "git", "mcp", "pressure", "pulse", "skills", "status"],
        )
        self.assertEqual(payload["overall"], "green")
        self.assertEqual(payload["blocked_conditions"], [])

    def test_warning_state_is_yellow_with_explicit_conditions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = self._collect(
                root,
                doctor=[CheckResult("pass", "a", "ok"), CheckResult("warn", "skill-repo-install", "extra")],
                status=_status_payload(pressure_warnings=["disk free below 10 GiB"]),
                skills=_skills_payload(),
                mcp=_mcp_payload(),
                dirty=["a.py", "b.py"],
            )
        self.assertEqual(payload["overall"], "yellow")
        joined = " ".join(payload["blocked_conditions"])
        self.assertIn("doctor", joined)
        self.assertIn("pressure", joined)
        self.assertIn("git", joined)

    def test_failure_state_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = self._collect(
                root,
                doctor=[CheckResult("fail", "manifest-alignment", "drift")],
                status=_status_payload(),
                skills=_skills_payload(),
                mcp=_mcp_payload(),
                dirty=[],
            )
        self.assertEqual(payload["overall"], "red")
        self.assertTrue(any("doctor" in c for c in payload["blocked_conditions"]))

    def test_mcp_invalid_and_skill_issues_drive_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = self._collect(
                root,
                doctor=[CheckResult("pass", "a", "ok")],
                status=_status_payload(),
                skills=_skills_payload(issues=2),
                mcp=_mcp_payload(invalid_configs=1),
                dirty=[],
            )
        self.assertEqual(payload["overall"], "red")
        conditions = " ".join(payload["blocked_conditions"])
        self.assertIn("mcp", conditions)
        self.assertIn("skills", conditions)

    def test_pulse_not_running_is_an_explicit_gray_condition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = self._collect(
                root,
                doctor=[CheckResult("pass", "a", "ok")],
                status=_status_payload(),
                skills=_skills_payload(),
                mcp=_mcp_payload(),
                dirty=[],
                write_pulse_state=False,
            )
        self.assertFalse(payload["sections"]["pulse"]["state_file_present"])
        self.assertTrue(any("pulse" in c for c in payload["blocked_conditions"]))
        self.assertEqual(payload["overall"], "yellow")

    def test_markdown_and_text_renderers_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = self._collect(
                root,
                doctor=[CheckResult("pass", "a", "ok")],
                status=_status_payload(),
                skills=_skills_payload(),
                mcp=_mcp_payload(),
                dirty=[],
            )
        md = runtime_evidence_markdown(payload)
        self.assertIn("Runtime evidence packet", md)
        self.assertIn("overall", md)
        out = StringIO()
        with redirect_stdout(out):
            print_runtime_evidence_text(payload)
        self.assertIn("runtime evidence:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
