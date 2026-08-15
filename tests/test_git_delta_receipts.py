"""Tests for ``sbp git --delta`` and the reconcile receipt join.

Hermetic throughout: every cache read/write happens under a TemporaryDirectory
via ``SKILLBOX_STATE_ROOT``; the receipts store is a fixture directory pointed
at by ``SKILLBOX_RECONCILE_RECEIPTS_DIR``; wrapper-level tests subprocess the
real ``scripts/sbp`` against a temp estate with pinned git config, so the
suite never scans the operator's ~/repos and never touches the real state
root or any real receipts store.

Byte-identity pins live in test_git_estate_goldens (default output) -- this
file proves the ADDITIVE surfaces: the ``delta`` object exists only under
``--delta``, and ``last_reconcile`` fields exist only when a receipts store
is present.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import git_estate, git_scan_cache  # noqa: E402

SBP = ROOT / "scripts" / "sbp"


def _row(path: str, band: str = "clean", **overrides) -> dict:
    row = {
        "path": path,
        "branch": "main",
        "ahead": 0,
        "behind": 0,
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "stash_count": 0,
        "classes": [],
        "risk_band": band,
        "registration": "unknown",
        "fix": [],
    }
    row.update(overrides)
    return row


def _envelope(rows: list[dict] | None = None, **overrides) -> dict:
    payload = {
        "schema": git_scan_cache.CACHE_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "roots": ["/tmp/estate"],
        "cwd_repo": None,
        "filters": [],
        "notes": [],
        "ignored_count": 0,
        "registry_applied": False,
        "repos": rows or [],
        "summary": {},
        "registration_summary": {
            "registered": 0,
            "unregistered": 0,
            "unknown": 0,
            "stale_registered": 0,
        },
        "stale_registered": [],
        "repo_count": len(rows or []),
        "elapsed_seconds": 0.1,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# compute_scan_delta -- pure envelope diff
# --------------------------------------------------------------------------- #


class ComputeScanDeltaTests(unittest.TestCase):
    def test_newly_bands_resolved_appeared_disappeared(self) -> None:
        baseline = _envelope(
            [
                _row("/e/a", "clean"),      # -> dirty      = newly dirty
                _row("/e/b", "clean"),      # -> ahead      = newly ahead
                _row("/e/c", "dirty"),      # -> mid-op     = newly mid-op
                _row("/e/d", "clean"),      # -> diverged   = newly diverged
                _row("/e/e", "clean"),      # -> blocked    = newly blocked
                _row("/e/f", "dirty"),      # -> clean      = resolved
                _row("/e/g", "ahead"),      # -> gone       = resolved + disappeared
                _row("/e/h", "clean"),      # -> gone       = disappeared only
                _row("/e/i", "dirty"),      # -> dirty      = unchanged, not reported
            ]
        )
        current = _envelope(
            [
                _row("/e/a", "dirty"),
                _row("/e/b", "ahead"),
                _row("/e/c", "mid-op"),
                _row("/e/d", "diverged"),
                _row("/e/e", "blocked"),
                _row("/e/f", "clean"),
                _row("/e/i", "dirty"),
                _row("/e/new", "clean"),    # appeared
            ]
        )
        delta = git_estate.compute_scan_delta(current, baseline)
        self.assertTrue(delta["available"])
        self.assertEqual(delta["baseline_written_at"], baseline["generated_at"])
        self.assertEqual(
            delta["newly"],
            {
                "dirty": ["/e/a"],
                "ahead": ["/e/b"],
                "mid-op": ["/e/c"],
                "diverged": ["/e/d"],
                "blocked": ["/e/e"],
            },
        )
        self.assertEqual(delta["resolved"], ["/e/f", "/e/g"])
        self.assertEqual(delta["appeared"], ["/e/new"])
        self.assertEqual(delta["disappeared"], ["/e/g", "/e/h"])
        self.assertEqual(delta["notes"], [])

    def test_no_changes_yields_empty_sections(self) -> None:
        rows = [_row("/e/a", "dirty"), _row("/e/b", "clean")]
        delta = git_estate.compute_scan_delta(_envelope(rows), _envelope(rows))
        self.assertEqual(delta["newly"], {})
        self.assertEqual(delta["resolved"], [])
        self.assertEqual(delta["appeared"], [])
        self.assertEqual(delta["disappeared"], [])

    def test_appeared_rows_never_count_as_newly(self) -> None:
        delta = git_estate.compute_scan_delta(
            _envelope([_row("/e/new", "dirty")]), _envelope([])
        )
        self.assertEqual(delta["newly"], {})
        self.assertEqual(delta["appeared"], ["/e/new"])

    def test_roots_and_filters_mismatch_are_noted_but_diff_still_shown(self) -> None:
        baseline = _envelope(
            [_row("/e/a", "dirty")], roots=["/other"], filters=["dirty"]
        )
        current = _envelope([_row("/e/a", "clean")])
        delta = git_estate.compute_scan_delta(current, baseline)
        self.assertEqual(delta["resolved"], ["/e/a"], "diff must still be computed")
        self.assertEqual(
            delta["notes"],
            [
                "delta baseline used different roots",
                "delta baseline used different --only filters",
            ],
        )

    def test_tty_delta_banner_renders_names_and_age(self) -> None:
        now = datetime.now(timezone.utc)
        baseline = _envelope(
            [_row("/e/a", "clean"), _row("/e/c", "dirty")],
            generated_at=(now - timedelta(minutes=12)).isoformat(),
        )
        current = _envelope(
            [_row("/e/a", "dirty"), _row("/e/b", "dirty"), _row("/e/c", "clean")],
            generated_at=now.isoformat(),
        )
        current["repos"].sort(key=lambda row: row["path"])
        report = dict(current)
        report["delta"] = git_estate.compute_scan_delta(current, baseline)
        text = "\n".join(git_estate.report_text_lines(report))
        self.assertIn("delta vs scan 12m ago:", text)
        self.assertIn("1 newly dirty (a)", text)
        self.assertIn("1 resolved (c)", text)
        self.assertIn("1 appeared (b)", text)

    def test_tty_unavailable_banner(self) -> None:
        report = _envelope()
        report["delta"] = {"available": False, "reason": "no previous scan"}
        text = "\n".join(git_estate.report_text_lines(report))
        self.assertIn("delta unavailable: no previous scan", text)


# --------------------------------------------------------------------------- #
# CLI handler -- baseline seeding, unavailability, --cached rejection
# --------------------------------------------------------------------------- #


class DeltaHandlerCase(unittest.TestCase):
    """Temp state root + the git-status handler with a mocked scan."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="git-delta-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.state_root = self.tmp / "state"
        patcher = mock.patch.dict(
            os.environ,
            {
                "SKILLBOX_STATE_ROOT": str(self.state_root),
                "SKILLBOX_RECONCILE_RECEIPTS_DIR": str(self.tmp / "no-receipts"),
                "SKILLBOX_AMP_CAPSULE_GUARD": str(self.tmp / "no-capsule-guard"),
                "SKILLBOX_AMP_CAMPAIGN_GUARD": str(self.tmp / "no-campaign-guard"),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _args(self, **overrides) -> Namespace:
        base = dict(
            format="json",
            cwd=str(self.tmp),
            only=[],
            roots=[],
            depth=2,
            cached=False,
            delta=False,
        )
        base.update(overrides)
        return Namespace(**base)

    def _run_handler(self, cli, args) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli._handle_git_status(args, ROOT)
        return code, out.getvalue(), err.getvalue()

    def _scan(self, cli, envelope, **arg_overrides) -> tuple[int, str, str]:
        with mock.patch.object(cli, "git_estate_report", return_value=envelope):
            return self._run_handler(cli, self._args(**arg_overrides))


class DeltaHandlerTests(DeltaHandlerCase):
    def test_delta_diffs_against_the_pre_run_generation(self) -> None:
        from runtime_manager import cli

        first = _envelope([_row("/e/a", "clean"), _row("/e/b", "dirty")])
        second = _envelope([_row("/e/a", "dirty"), _row("/e/b", "clean")])

        code, out, _ = self._scan(cli, first)
        self.assertEqual(code, 0)
        self.assertNotIn("delta", json.loads(out), "no --delta -> no delta key")

        code, out, _ = self._scan(cli, second, delta=True)
        self.assertEqual(code, 0)
        delta = json.loads(out)["delta"]
        self.assertTrue(delta["available"])
        self.assertEqual(delta["baseline_written_at"], first["generated_at"])
        self.assertEqual(delta["newly"], {"dirty": ["/e/a"]})
        self.assertEqual(delta["resolved"], ["/e/b"])

    def test_cached_envelope_never_embeds_the_delta(self) -> None:
        from runtime_manager import cli

        self._scan(cli, _envelope([_row("/e/a", "clean")]))
        self._scan(cli, _envelope([_row("/e/a", "dirty")]), delta=True)
        current = git_scan_cache.load_scan_cache()
        self.assertIsNotNone(current)
        self.assertNotIn("delta", current[0])

    def test_no_previous_scan_is_loud_in_json_and_tty(self) -> None:
        from runtime_manager import cli

        envelope = _envelope([_row("/e/a", "dirty")])
        code, out, _ = self._scan(cli, envelope, delta=True)
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out)["delta"],
            {"available": False, "reason": "no previous scan"},
        )

        # Fresh state root again for the tty variant.
        with mock.patch.dict(
            os.environ, {"SKILLBOX_STATE_ROOT": str(self.tmp / "state2")}
        ):
            code, out, _ = self._scan(cli, envelope, delta=True, format="text")
        self.assertEqual(code, 0)
        self.assertIn("delta unavailable: no previous scan", out)

    def test_cached_plus_delta_is_a_usage_error_and_never_scans(self) -> None:
        from runtime_manager import cli

        with mock.patch.object(
            cli, "git_estate_report", side_effect=AssertionError("scanned")
        ), mock.patch.object(
            cli, "load_git_scan_cache", side_effect=AssertionError("read cache")
        ):
            code, out, err = self._run_handler(
                cli, self._args(cached=True, delta=True)
            )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--delta needs a live scan", err)

    def test_roots_mismatch_note_flows_through_the_handler(self) -> None:
        from runtime_manager import cli

        self._scan(cli, _envelope([_row("/e/a", "dirty")], roots=["/somewhere/else"]))
        code, out, _ = self._scan(
            cli, _envelope([_row("/e/a", "dirty")]), delta=True
        )
        self.assertEqual(code, 0)
        delta = json.loads(out)["delta"]
        self.assertIn("delta baseline used different roots", delta["notes"])

    def test_delta_survives_a_failed_cache_write(self) -> None:
        from runtime_manager import cli

        first = _envelope([_row("/e/a", "clean")])
        self._scan(cli, first)
        second = _envelope([_row("/e/a", "dirty")])
        with mock.patch.object(
            cli, "git_estate_report", return_value=second
        ), mock.patch.object(
            cli, "write_git_scan_cache", side_effect=OSError("read-only fs")
        ):
            code, out, err = self._run_handler(cli, self._args(delta=True))
        self.assertEqual(code, 0)
        self.assertIn("cache write failed", err)
        delta = json.loads(out)["delta"]
        self.assertTrue(delta["available"], "baseline is loaded before the write")
        self.assertEqual(delta["newly"], {"dirty": ["/e/a"]})


# --------------------------------------------------------------------------- #
# Reconcile receipt join -- fixture store, real mini estate
# --------------------------------------------------------------------------- #


class ReceiptsCase(unittest.TestCase):
    """Temp estate with one dirty repo + a fixture receipts store."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="git-receipts-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.estate = self.tmp / "estate"
        self.estate.mkdir()
        self.receipts = self.tmp / "receipts"
        self.receipts.mkdir()
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text(
            "[user]\n\temail = fixture@example.invalid\n\tname = Receipts Fixture\n"
            "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n",
            encoding="utf-8",
        )
        patcher = mock.patch.dict(
            os.environ,
            {
                "SKILLBOX_STATE_ROOT": str(self.tmp / "state"),
                "SKILLBOX_CONFIG_ROOT": str(self.tmp / "config"),  # registry degrades
                "SKILLBOX_RECONCILE_RECEIPTS_DIR": str(self.receipts),
                "SKILLBOX_AMP_CAPSULE_GUARD": str(self.tmp / "no-capsule-guard"),
                "SKILLBOX_AMP_CAMPAIGN_GUARD": str(self.tmp / "no-campaign-guard"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(gitconfig),
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dirty = self.make_dirty_repo("b-dirty")
        self.other = self.make_dirty_repo("c-other")

    def make_dirty_repo(self, name: str) -> Path:
        repo = self.estate / name
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(repo)],
            check=True,
            capture_output=True,
            env=os.environ.copy(),
        )
        (repo / "loose.txt").write_text("loose\n", encoding="utf-8")
        return repo

    def write_receipt(
        self,
        name: str,
        subject_id: str,
        *,
        age_days: float = 1.0,
        state: str = "passed",
    ) -> str:
        created = (
            datetime.now(timezone.utc) - timedelta(days=age_days)
        ).isoformat()
        payload = {
            "schema_version": "1.0.0",
            "receipt_id": name,
            "receipt_type": "final_proof",
            "state": state,
            "created_at": created,
            "subject": {"kind": "repo", "id": subject_id},
        }
        (self.receipts / f"{name}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        return created

    def build(self, **overrides) -> dict:
        kwargs = dict(roots=[str(self.estate)], depth=2, cwd=str(self.dirty))
        kwargs.update(overrides)
        return git_estate.build_report(**kwargs)

    def row_for(self, report: dict, repo: Path) -> dict:
        for row in report["repos"]:
            if row["path"] == str(repo):
                return row
        raise AssertionError(f"no row for {repo}")


class ReceiptsJoinTests(ReceiptsCase):
    def test_fresh_receipt_joins_by_repo_id_without_marker(self) -> None:
        created = self.write_receipt("r1", "b-dirty", age_days=2)
        report = self.build()
        self.assertEqual(self.row_for(report, self.dirty)["last_reconcile"], created)
        text = "\n".join(git_estate.report_text_lines(report))
        self.assertIn("last safe sync: 2d ago (reconcile receipt)", text)
        self.assertNotIn("[last safe sync", text, "fresh receipt -> no glance marker")

    def test_stale_receipt_on_non_clean_row_gets_the_glance_marker(self) -> None:
        self.write_receipt("r1", "b-dirty", age_days=45)
        report = self.build()
        text = "\n".join(git_estate.report_text_lines(report))
        marker_lines = [
            line
            for line in text.splitlines()
            if str(self.dirty) in line and "[last safe sync 45d ago]" in line
        ]
        self.assertTrue(marker_lines, text)

    def test_missing_receipt_is_null_and_blank_with_zero_notes(self) -> None:
        self.write_receipt("r1", "b-dirty", age_days=2)
        report = self.build()
        self.assertIsNone(self.row_for(report, self.other)["last_reconcile"])
        self.assertEqual(
            [n for n in report["notes"] if "reconcile receipts" in n],
            [],
            "no note spam",
        )

    def test_join_matches_full_path_subject_ids_too(self) -> None:
        created = self.write_receipt("r1", str(self.dirty), age_days=1)
        report = self.build()
        self.assertEqual(self.row_for(report, self.dirty)["last_reconcile"], created)

    def test_failed_and_skipped_receipts_never_count_as_safe_sync(self) -> None:
        self.write_receipt("r1", "b-dirty", age_days=1, state="failed")
        self.write_receipt("r2", "b-dirty", age_days=1, state="skipped")
        report = self.build()
        self.assertIsNone(self.row_for(report, self.dirty)["last_reconcile"])

    def test_newest_passed_receipt_wins(self) -> None:
        self.write_receipt("older", "b-dirty", age_days=40)
        newest = self.write_receipt("newer", "b-dirty", age_days=3)
        report = self.build()
        self.assertEqual(self.row_for(report, self.dirty)["last_reconcile"], newest)

    def test_malformed_receipt_files_are_skipped_silently(self) -> None:
        (self.receipts / "broken.json").write_text("{torn", encoding="utf-8")
        (self.receipts / "notes.txt").write_text("not a receipt", encoding="utf-8")
        created = self.write_receipt("r1", "b-dirty", age_days=2)
        report = self.build()
        self.assertEqual(self.row_for(report, self.dirty)["last_reconcile"], created)
        self.assertEqual(
            [n for n in report["notes"] if "reconcile receipts" in n], []
        )

    def test_absent_store_adds_nothing_at_all(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"SKILLBOX_RECONCILE_RECEIPTS_DIR": str(self.tmp / "no-such-dir")},
        ):
            report = self.build()
        for row in report["repos"] + [report["cwd_repo"]]:
            self.assertNotIn(
                "last_reconcile", row, "absent store must stay byte-identical"
            )
        self.assertEqual(
            [n for n in report["notes"] if "reconcile receipts" in n], []
        )
        text = "\n".join(git_estate.report_text_lines(report))
        self.assertNotIn("safe sync", text)

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_unreadable_store_is_exactly_one_note(self) -> None:
        self.receipts.chmod(0o000)
        self.addCleanup(self.receipts.chmod, 0o700)
        report = self.build()
        receipt_notes = [n for n in report["notes"] if "reconcile receipts" in n]
        self.assertEqual(len(receipt_notes), 1, report["notes"])
        self.assertIn("unavailable", receipt_notes[0])
        self.assertIsNone(self.row_for(report, self.dirty)["last_reconcile"])

    def test_cwd_detail_row_is_joined_too(self) -> None:
        created = self.write_receipt("r1", "b-dirty", age_days=2)
        report = self.build()
        self.assertEqual(report["cwd_repo"]["last_reconcile"], created)

    def test_receipt_lookup_never_spawns_a_process(self) -> None:
        """The join is a file read, never git or the reconcile skill."""
        self.write_receipt("r1", "b-dirty", age_days=2)
        boom = AssertionError("receipts join spawned a subprocess")
        with mock.patch.object(subprocess, "Popen", side_effect=boom):
            index, error, present = git_estate._load_reconcile_receipts()
        self.assertTrue(present)
        self.assertIsNone(error)
        self.assertIn("b-dirty", index)


# --------------------------------------------------------------------------- #
# Wrapper passthrough -- the real scripts/sbp front door
# --------------------------------------------------------------------------- #


class WrapperDeltaTests(ReceiptsCase):
    """--delta through the real wrapper: passthrough, delta, and rejection."""

    def run_sbp(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "SKILLBOX_ROOT": str(ROOT),
            "SKILLBOX_INVOKE_CWD": str(self.estate),
        }
        return subprocess.run(
            [str(SBP), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env=env,
        )

    def scan_args(self) -> list[str]:
        return ["--root", str(self.estate), "--depth", "2"]

    def test_delta_end_to_end_first_run_loud_second_run_diffs(self) -> None:
        # First --delta run: no previous generation -> loud one-liner, exit 0.
        first = self.run_sbp("git", "--delta", *self.scan_args())
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("delta unavailable: no previous scan", first.stdout)

        # Change one repo's band (drop its untracked file: dirty ->
        # no-remote, these local-only fixtures have no upstream), then
        # --delta --json must report the transition.
        (self.other / "loose.txt").unlink()
        second = self.run_sbp("git", "--delta", "--json", *self.scan_args())
        self.assertEqual(second.returncode, 0, second.stderr)
        delta = json.loads(second.stdout)["delta"]
        self.assertTrue(delta["available"])
        self.assertEqual(delta["newly"], {"no-remote": [str(self.other)]})
        self.assertEqual(delta["resolved"], [], "no-remote is not clean")
        self.assertEqual(delta["appeared"], [])
        self.assertEqual(delta["disappeared"], [])

    def test_cached_delta_is_refused_with_exit_2(self) -> None:
        refused = self.run_sbp("git", "--cached", "--delta")
        self.assertEqual(refused.returncode, 2, refused.stdout)
        self.assertIn("--delta needs a live scan", refused.stderr)


if __name__ == "__main__":
    unittest.main()
