"""Amp guard joins on the ``sbp git`` envelope (git_estate).

Both joins are *delegations* to the reconcile skill's guard scripts, so the
tests point ``SKILLBOX_AMP_CAPSULE_GUARD`` / ``SKILLBOX_AMP_CAMPAIGN_GUARD``
at tiny bash stand-ins emitting canned JSON. Contracts under test mirror the
reconcile-receipts join: guard script absent -> the default capsule join adds
NOTHING (byte-identical envelope); a present-but-failing guard -> exactly ONE
note; verdict rows stamp additive fields only. ``--amp`` is opt-in, so there
an absent guard IS loud, and an authority error never row-spams
``indeterminate``.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import git_estate  # noqa: E402

from tests.test_git_estate import GitEstateFixtureCase  # noqa: E402


class _AmpGuardCase(GitEstateFixtureCase):
    """Fixture helpers for fake guard scripts (invoked via ``bash <script>``)."""

    def write_guard(
        self,
        name: str,
        payload: dict | None,
        *,
        exit_code: int = 1,
        stderr: str = "",
    ) -> Path:
        """A guard stand-in: record the invocation, emit ``payload``, exit."""
        script = self.tmp / name
        marker = self.marker_for(script)
        body = [f"echo invoked >> {marker}"]
        if payload is not None:
            payload_file = self.tmp / f"{name}.json"
            payload_file.write_text(json.dumps(payload), encoding="utf-8")
            body.append(f'cat "{payload_file}"')
        if stderr:
            body.append(f"echo {stderr} >&2")
        body.append(f"exit {exit_code}")
        script.write_text("\n".join(body) + "\n", encoding="utf-8")
        return script

    def marker_for(self, script: Path) -> Path:
        return Path(f"{script}.invoked")

    def use_capsule_guard(self, script: Path) -> None:
        patcher = mock.patch.dict(
            os.environ, {git_estate._AMP_CAPSULE_ENV: str(script)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def use_campaign_guard(self, script: Path) -> None:
        patcher = mock.patch.dict(
            os.environ, {git_estate._AMP_CAMPAIGN_ENV: str(script)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def amp_notes(self, report: dict) -> list[str]:
        return [note for note in report.get("notes") or [] if "amp" in note]


class CapsuleJoinTests(_AmpGuardCase):
    """Default-on capsule join (no flag)."""

    def test_absent_guard_adds_nothing(self) -> None:
        self.make_repo("a-plain")
        self.write_config_fixture()
        report = git_estate.build_report(roots=[str(self.estate)], depth=2, cwd=None)
        self.assertNotIn("amp", report)
        self.assertEqual(self.amp_notes(report), [])
        self.assertTrue(
            all("amp_capsule" not in row for row in report["repos"])
        )

    def test_broken_capsule_row_stamped_marked_and_kept_visible(self) -> None:
        clean = self.make_clean_clone("a-sealed")
        self.write_config_fixture(repos=[{"id": "a-sealed", "path": str(clean)}])
        guard = self.write_guard(
            "capsule-guard.sh",
            {
                "rows": [
                    {
                        "path": str(clean),
                        "verdict": "capsule-broken-published",
                        "reasons": ["published plane drifted"],
                    }
                ],
                "drift_rows": 1,
            },
        )
        self.use_capsule_guard(guard)

        report = git_estate.build_report(roots=[str(self.estate)], depth=2, cwd=None)

        row = next(r for r in report["repos"] if r["path"] == str(clean))
        self.assertEqual(row["amp_capsule"], "capsule-broken-published")
        self.assertEqual(row["risk_band"], "clean")
        reseal_fixes = [f for f in row["fix"] if "amp_capsule_reseal.py" in f]
        self.assertEqual(len(reseal_fixes), 1)
        self.assertIn("reconcile skill", reseal_fixes[0])
        self.assertEqual(
            report["amp"]["capsule"],
            {"applied": True, "source": str(guard), "flagged_rows": 1},
        )

        text = "\n".join(git_estate.report_text_lines(report))
        # The clean row stays visible (Orb drift hides behind a clean HEAD).
        self.assertIn("[capsule-broken-published]", text)
        self.assertIn("amp: capsule guard joined (1 rows flagged)", text)
        self.assertIn("- amp-capsule: 1", text)
        self.assertIn("amp_capsule_reseal.py", text)

    def test_quiet_verdicts_are_not_stamped(self) -> None:
        clean = self.make_clean_clone("a-quiet")
        self.write_config_fixture(repos=[{"id": "a-quiet", "path": str(clean)}])
        guard = self.write_guard(
            "capsule-guard.sh",
            {
                "rows": [
                    {"path": str(clean), "verdict": "capsule-clear", "reasons": []},
                ],
                "drift_rows": 0,
            },
            exit_code=0,
        )
        self.use_capsule_guard(guard)
        report = git_estate.build_report(roots=[str(self.estate)], depth=2, cwd=None)
        self.assertTrue(all("amp_capsule" not in row for row in report["repos"]))
        self.assertEqual(report["amp"]["capsule"]["flagged_rows"], 0)
        self.assertNotIn("- amp-capsule:", "\n".join(git_estate.report_text_lines(report)))

    def test_guard_failure_is_exactly_one_note(self) -> None:
        self.make_repo("a-plain")
        self.write_config_fixture()
        guard = self.write_guard(
            "capsule-guard.sh", None, exit_code=2, stderr="registry sad"
        )
        self.use_capsule_guard(guard)

        report = git_estate.build_report(roots=[str(self.estate)], depth=2, cwd=None)

        notes = self.amp_notes(report)
        self.assertEqual(len(notes), 1)
        self.assertIn("amp capsule guard unavailable", notes[0])
        self.assertIn("registry sad", notes[0])
        self.assertFalse(report["amp"]["capsule"]["applied"])
        self.assertTrue(all("amp_capsule" not in row for row in report["repos"]))


class CampaignJoinTests(_AmpGuardCase):
    """--amp opt-in campaign/lease join."""

    def test_without_flag_guard_is_never_invoked(self) -> None:
        self.make_repo("a-plain")
        self.write_config_fixture()
        guard = self.write_guard("campaign-guard.sh", {"rows": []}, exit_code=0)
        self.use_campaign_guard(guard)

        report = git_estate.build_report(roots=[str(self.estate)], depth=2, cwd=None)

        self.assertFalse(self.marker_for(guard).exists())
        self.assertNotIn("campaign", report.get("amp") or {})

    def test_flag_joins_lease_verdicts(self) -> None:
        clean = self.make_clean_clone("a-leased")
        self.write_config_fixture(repos=[{"id": "a-leased", "path": str(clean)}])
        guard = self.write_guard(
            "campaign-guard.sh",
            {
                "authority": {
                    "authority_environment_id": "d3",
                    "captured_at": "2026-08-15T12:00:00Z",
                },
                "authority_error": None,
                "active_leases": 1,
                "rows": [
                    {
                        "path": str(clean),
                        "verdict": "amp-leased",
                        "reasons": [
                            "active lease lease-1 state=held owner=writer-session/abc"
                        ],
                    }
                ],
            },
        )
        self.use_campaign_guard(guard)

        report = git_estate.build_report(
            roots=[str(self.estate)], depth=2, cwd=None, amp=True
        )

        self.assertTrue(self.marker_for(guard).exists())
        row = next(r for r in report["repos"] if r["path"] == str(clean))
        self.assertEqual(row["amp_verdict"], "amp-leased")
        self.assertEqual(len(row["amp_reasons"]), 1)
        self.assertTrue(any("dws-closeout" in f for f in row["fix"]))
        campaign = report["amp"]["campaign"]
        self.assertTrue(campaign["applied"])
        self.assertEqual(campaign["flagged_rows"], 1)
        self.assertEqual(campaign["active_leases"], 1)
        self.assertEqual(campaign["authority"]["environment"], "d3")

        text = "\n".join(git_estate.report_text_lines(report))
        self.assertIn("[amp-leased]", text)
        self.assertIn("amp: campaign guard joined (1 rows flagged, 1 active leases)", text)
        self.assertIn("- amp-campaign: 1", text)

    def test_authority_error_is_one_note_and_no_row_spam(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("x\n", encoding="utf-8")
        self.write_config_fixture()
        guard = self.write_guard(
            "campaign-guard.sh",
            {
                "authority": None,
                "authority_error": {"code": "ssh-unreachable", "detail": "d3 down"},
                "active_leases": 0,
                # The guard fails closed: every row shows indeterminate. The
                # join must NOT copy that spam onto the scan rows.
                "rows": [
                    {"path": str(dirty), "verdict": "indeterminate", "reasons": []}
                ],
            },
        )
        self.use_campaign_guard(guard)

        report = git_estate.build_report(
            roots=[str(self.estate)], depth=2, cwd=None, amp=True
        )

        notes = self.amp_notes(report)
        self.assertEqual(len(notes), 1)
        self.assertIn("amp authority unavailable: ssh-unreachable: d3 down", notes[0])
        self.assertFalse(report["amp"]["campaign"]["applied"])
        self.assertTrue(all("amp_verdict" not in row for row in report["repos"]))

    def test_flag_with_absent_guard_is_loud(self) -> None:
        self.make_repo("a-plain")
        self.write_config_fixture()
        # The fixture base pins the campaign guard env at a nonexistent path.
        report = git_estate.build_report(
            roots=[str(self.estate)], depth=2, cwd=None, amp=True
        )
        notes = self.amp_notes(report)
        self.assertEqual(len(notes), 1)
        self.assertIn("amp campaign guard unavailable", notes[0])
        self.assertFalse(report["amp"]["campaign"]["applied"])


class BacklogFooterTests(_AmpGuardCase):
    """Backlog routing to reconcile + divide-and-conquer."""

    def test_below_threshold_adds_nothing(self) -> None:
        dirty = self.make_repo("a-dirty")
        (dirty / "loose.txt").write_text("x\n", encoding="utf-8")
        self.write_config_fixture()
        report = git_estate.build_report(roots=[str(self.estate)], depth=2, cwd=None)
        self.assertNotIn("backlog", report)
        self.assertNotIn("backlog:", "\n".join(git_estate.report_text_lines(report)))

    def test_at_threshold_routes_to_reconcile_and_divide_and_conquer(self) -> None:
        for index in range(2):
            dirty = self.make_repo(f"dirty-{index}")
            (dirty / "loose.txt").write_text("x\n", encoding="utf-8")
        self.write_config_fixture()
        with mock.patch.object(git_estate, "_BACKLOG_THRESHOLD", 2):
            report = git_estate.build_report(
                roots=[str(self.estate)], depth=2, cwd=None
            )
        self.assertIn("2 issue rows", report["backlog"])
        text = "\n".join(git_estate.report_text_lines(report))
        self.assertIn("backlog: 2 issue rows — run the reconcile skill", text)
        self.assertIn("divide-and-conquer", text)

    def test_next_actions_truncation_names_hidden_row_count(self) -> None:
        base = {
            "staged": 0,
            "unstaged": 1,
            "untracked": 0,
            "ahead": 0,
            "behind": 0,
            "stash_count": 0,
            "branch": "main",
            "risk_band": "dirty",
        }
        rows = [
            {**base, "path": f"/estate/r{index}", "fix": [f"fix r{index}"]}
            for index in range(7)
        ]
        report = {"generated_at": "2026-08-15T12:00:00+00:00", "repos": rows}
        text = "\n".join(git_estate.report_text_lines(report))
        self.assertIn("(… 2 more issue rows — sbp git --json for the full set)", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
