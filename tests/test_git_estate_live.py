"""Hermetic tests for ``sbp git --live`` (origin comparison delegation).

``--live`` never probes origins itself: it runs the reconcile skill's
``fleet_convergence.py`` as a subprocess and joins its per-checkout verdicts
back onto scanned rows by path. Everything here is exercised against a FAKE
fleet_convergence script injected via ``$SKILLBOX_FLEET_CONVERGENCE`` over a
small real-git fixture estate -- no network, no skills-private checkout:

* the annotation join on matching paths (unmatched rows stay local-only);
* a locally-clean repo whose live origin moved -> ``origin-newer`` (plus the
  tty marker that keeps the row visible despite the clean-row fold);
* every degrade path (absent script, overall timeout, unparseable output,
  unexpected exit) -> one loud ``live comparison unavailable`` note,
  local-only rows, exit 0;
* the default (no ``--live``) envelope carries NO live fields and spawns no
  delegation subprocess -- byte-identical to a degraded --live run modulo
  ``generated_at``/``elapsed_seconds`` and the additive live note/object;
* wrapper passthrough: ``scripts/sbp git --live --json`` reaches manage.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ENV_MANAGER_DIR = ROOT / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import git_estate  # noqa: E402

SCRIPT_ENV = "SKILLBOX_FLEET_CONVERGENCE"
TIMEOUT_ENV = "SKILLBOX_FLEET_CONVERGENCE_TIMEOUT_S"


def fleet_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Minimal well-formed fleet_convergence --json payload."""
    return {
        "environments_probed": ["local"],
        "environments_unreachable": {},
        "environments_skipped_no_roots": [],
        "repos": entries,
        "converged": not entries,
        "status": "blocked" if entries else "clean",
        "exit_code": 1 if entries else 0,
        "reason_codes": [],
    }


def repo_entry(
    path: str,
    *,
    mismatch: bool,
    origin_head: str | None = "aaaaaaaaa",
    host: str = "local",
) -> dict[str, Any]:
    """One fleet payload repo row for a single-host checkout.

    ``origin_head=None`` models a checkout whose live origin head could not
    be observed (no remote, or the live origin probe failed).
    """
    local_head = "bbbbbbbbb" if mismatch else origin_head
    problems = (
        [f"origin-mismatch({origin_head[:7]}):{host}"] if mismatch else []
    )
    return {
        "repo": f"test/{Path(path).name}",
        "operator_owned": True,
        "boxes": [host],
        "heads": {host: local_head} if local_head else {},
        "origin_head": origin_head,
        "verdict": "DIVERGED" if mismatch else "converged",
        "problems": problems,
        "paths": [
            {
                "host": host,
                "path": path,
                "role": Path(path).name,
                "canonical_id": None,
                "aliases": [],
                "presence": "discovered-unregistered",
                "worktree_kind": "primary",
                "origin": None,
                "head": local_head,
                "branch": "main",
                "reason_codes": [],
            }
        ],
        "reason_codes": [],
    }


class LiveFixtureCase(unittest.TestCase):
    """Temp estate of real git repos + a fake fleet_convergence script."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="git-estate-live-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.estate = self.tmp / "estate"
        self.estate.mkdir()
        self.origins = self.tmp / "origins"
        self.origins.mkdir()

        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text(
            "[user]\n"
            "\temail = fixture@example.invalid\n"
            "\tname = Git Estate Live Fixture\n"
            "[init]\n"
            "\tdefaultBranch = main\n"
            "[commit]\n"
            "\tgpgsign = false\n",
            encoding="utf-8",
        )
        patcher = mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(gitconfig),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                # Absent config root -> deterministic 'registry unavailable'
                # note in every report; live tests don't care about registry.
                "SKILLBOX_CONFIG_ROOT": str(self.tmp / "config"),
                "SKILLBOX_STATE_ROOT": str(self.tmp / "state"),
                # Never let a test accidentally reach the real reconcile
                # checkout; every live test overrides this deliberately.
                SCRIPT_ENV: str(self.tmp / "no-such-fleet-convergence.py"),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- git fixture helpers (same conventions as tests/test_git_estate.py) --

    def git(self, cwd: Path, *args: str) -> None:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"git {' '.join(args)} failed in {cwd}:\n{proc.stdout}\n{proc.stderr}"
            )

    def make_repo(self, name: str, *, parent: Path | None = None) -> Path:
        repo = (parent or self.estate) / name
        repo.mkdir(parents=True)
        self.git(repo, "init", "-q", "-b", "main")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git(repo, "add", "tracked.txt")
        self.git(repo, "commit", "-q", "-m", "base")
        return repo

    def make_clean_clone(self, name: str) -> Path:
        origin = self.make_repo(f"{name}-origin", parent=self.origins)
        clone = self.estate / name
        self.git(self.tmp, "clone", "-q", f"file://{origin}", str(clone))
        return clone

    def make_ahead_clone(self, name: str) -> Path:
        clone = self.make_clean_clone(name)
        (clone / "local.txt").write_text("local\n", encoding="utf-8")
        self.git(clone, "add", "local.txt")
        self.git(clone, "commit", "-q", "-m", "local work")
        return clone

    # -- fake fleet_convergence helpers --

    def install_fake_fleet(
        self,
        payload: dict[str, Any] | None = None,
        *,
        body: str | None = None,
        exit_code: int = 1,
    ) -> Path:
        """Write a fake fleet_convergence.py and point $SKILLBOX_FLEET_CONVERGENCE at it.

        ``payload`` mode prints the given JSON and records argv to
        ``self.argv_capture``; ``body`` mode installs arbitrary python source.
        """
        script = self.tmp / "fake_fleet_convergence.py"
        self.argv_capture = self.tmp / "fleet-argv.json"
        if body is None:
            payload_file = self.tmp / "fleet-payload.json"
            payload_file.write_text(json.dumps(payload or fleet_payload([])), encoding="utf-8")
            body = textwrap.dedent(
                f"""
                import json, sys
                with open({str(self.argv_capture)!r}, "w") as fh:
                    json.dump(sys.argv[1:], fh)
                with open({str(payload_file)!r}) as fh:
                    sys.stdout.write(fh.read())
                sys.exit({exit_code})
                """
            )
        script.write_text(body, encoding="utf-8")
        os.environ[SCRIPT_ENV] = str(script)
        return script

    def build(self, **kwargs: Any) -> dict[str, Any]:
        return git_estate.build_report(
            roots=[str(self.estate)], cwd=None, **kwargs
        )

    @staticmethod
    def rows_by_name(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {Path(row["path"]).name: row for row in report["repos"]}


class OriginStateTests(unittest.TestCase):
    """Pure mapping from (local cached counts, live head mismatch) -> state."""

    def test_mapping(self) -> None:
        state = git_estate._origin_state
        self.assertEqual(state(0, 0, False), "origin-current")
        self.assertEqual(state(3, 0, False), "origin-current")
        self.assertEqual(state(0, 0, True), "origin-newer")
        self.assertEqual(state(0, 2, True), "behind-origin")
        self.assertEqual(state(1, 2, True), "diverged-from-origin")
        self.assertEqual(state(1, 0, True), "origin-differs")

    def test_drift_vocabulary_matches_constant(self) -> None:
        produced = {
            git_estate._origin_state(ahead, behind, True)
            for ahead in (0, 1)
            for behind in (0, 1)
        }
        self.assertEqual(produced, set(git_estate.LIVE_DRIFT_STATES))


class LiveAnnotationTests(LiveFixtureCase):
    def test_annotation_joins_matching_paths_and_skips_unmatched(self) -> None:
        clean = self.make_clean_clone("clean-repo")
        ahead = self.make_ahead_clone("ahead-repo")
        self.make_repo("solo-repo")  # no remote, deliberately unmatched
        self.install_fake_fleet(
            fleet_payload(
                [
                    repo_entry(os.path.realpath(clean), mismatch=True),
                    repo_entry(os.path.realpath(ahead), mismatch=True),
                ]
            )
        )
        report = self.build(live=True)
        self.assertEqual(
            report["live"], {"applied": True, "source": os.environ[SCRIPT_ENV], "matched_rows": 2}
        )
        rows = self.rows_by_name(report)

        self.assertEqual(rows["clean-repo"]["origin_state"], "origin-newer")
        self.assertEqual(rows["clean-repo"]["origin_head"], "aaaaaaaaa")
        self.assertIn(
            f"git -C {rows['clean-repo']['path']} pull --ff-only  # origin has newer (live)",
            rows["clean-repo"]["fix"],
        )
        self.assertEqual(rows["ahead-repo"]["origin_state"], "origin-differs")
        # Unmatched rows silently keep their local-only shape.
        self.assertNotIn("origin_state", rows["solo-repo"])
        self.assertNotIn("origin_head", rows["solo-repo"])
        # Delegation used the machine surface with a bounded probe timeout.
        argv = json.loads(self.argv_capture.read_text(encoding="utf-8"))
        self.assertIn("--json", argv)
        self.assertIn("--all", argv)
        self.assertIn("--timeout", argv)

    def test_matched_current_row_annotates_origin_current(self) -> None:
        clean = self.make_clean_clone("clean-repo")
        self.install_fake_fleet(
            fleet_payload([repo_entry(os.path.realpath(clean), mismatch=False)]),
            exit_code=0,
        )
        report = self.build(live=True)
        row = self.rows_by_name(report)["clean-repo"]
        self.assertEqual(row["origin_state"], "origin-current")
        # Current rows fold away like any clean row: no live marker, no row.
        text = "\n".join(git_estate.report_text_lines(report))
        self.assertNotIn("[origin-current]", text)
        self.assertIn("live: origin comparison applied", text)

    def test_matched_row_without_live_head_is_origin_unknown(self) -> None:
        solo = self.make_repo("solo-repo")  # no remote -> no live origin head
        self.install_fake_fleet(
            fleet_payload(
                [repo_entry(os.path.realpath(solo), mismatch=False, origin_head=None)]
            ),
            exit_code=0,
        )
        report = self.build(live=True)
        row = self.rows_by_name(report)["solo-repo"]
        self.assertEqual(row["origin_state"], "origin-unknown")
        self.assertIsNone(row["origin_head"])
        # Not a drift state: no marker in the tty table.
        self.assertNotIn(
            "[origin-unknown]", "\n".join(git_estate.report_text_lines(report))
        )

    def test_tty_unfolds_clean_row_with_origin_newer_marker(self) -> None:
        clean = self.make_clean_clone("clean-repo")
        self.install_fake_fleet(
            fleet_payload([repo_entry(os.path.realpath(clean), mismatch=True)])
        )
        report = self.build(live=True)
        text = "\n".join(git_estate.report_text_lines(report))
        self.assertIn("live: origin comparison applied", text)
        row_lines = [line for line in text.splitlines() if "[origin-newer]" in line]
        self.assertEqual(len(row_lines), 1)
        self.assertIn(str(clean), row_lines[0])


class LiveDegradeTests(LiveFixtureCase):
    def assert_degraded(self, report: dict[str, Any], reason_fragment: str) -> None:
        self.assertFalse(report["live"]["applied"])
        self.assertIn(reason_fragment, report["live"]["reason"])
        live_notes = [
            note
            for note in report["notes"]
            if note.startswith("live comparison unavailable: ")
        ]
        self.assertEqual(len(live_notes), 1)
        self.assertIn(reason_fragment, live_notes[0])
        for row in report["repos"]:
            self.assertNotIn("origin_state", row)
            self.assertNotIn("origin_head", row)
        # The loud note reaches the tty rendering too.
        text = "\n".join(git_estate.report_text_lines(report))
        self.assertIn("note: live comparison unavailable: ", text)

    def test_absent_script_degrades_loudly(self) -> None:
        self.make_clean_clone("clean-repo")
        # setUp already points SCRIPT_ENV at a nonexistent file.
        report = self.build(live=True)
        self.assert_degraded(report, "fleet_convergence not found at ")

    def test_timeout_degrades_loudly(self) -> None:
        self.make_clean_clone("clean-repo")
        self.install_fake_fleet(body="import time\ntime.sleep(30)\n")
        os.environ[TIMEOUT_ENV] = "0.5"
        self.addCleanup(os.environ.pop, TIMEOUT_ENV, None)
        report = self.build(live=True)
        self.assert_degraded(report, "timed out after 0.5s")

    def test_unparseable_output_degrades_loudly(self) -> None:
        self.make_clean_clone("clean-repo")
        self.install_fake_fleet(body="print('this is not json')\n")
        report = self.build(live=True)
        self.assert_degraded(report, "unparseable output from ")

    def test_unexpected_exit_degrades_loudly(self) -> None:
        self.make_clean_clone("clean-repo")
        self.install_fake_fleet(
            body=(
                "import sys\n"
                "print('ERROR: fleet configuration is invalid', file=sys.stderr)\n"
                "sys.exit(2)\n"
            )
        )
        report = self.build(live=True)
        self.assert_degraded(report, "exited 2")

    def test_degraded_live_equals_default_modulo_additive_fields(self) -> None:
        self.make_clean_clone("clean-repo")
        self.make_ahead_clone("ahead-repo")
        default_report = self.build()
        degraded = self.build(live=True)

        def normalized(report: dict[str, Any]) -> dict[str, Any]:
            trimmed = json.loads(json.dumps(report))
            trimmed.pop("generated_at")
            trimmed.pop("elapsed_seconds")
            trimmed.pop("live", None)
            trimmed["notes"] = [
                note
                for note in trimmed["notes"]
                if not note.startswith("live comparison unavailable: ")
            ]
            return trimmed

        self.assertEqual(normalized(default_report), normalized(degraded))


class DefaultEnvelopeTests(LiveFixtureCase):
    def test_no_live_flag_means_no_live_fields_and_no_delegation(self) -> None:
        self.make_clean_clone("clean-repo")
        self.install_fake_fleet(fleet_payload([]))
        report = self.build()  # live absent
        self.assertNotIn("live", report)
        for row in report["repos"]:
            self.assertNotIn("origin_state", row)
            self.assertNotIn("origin_head", row)
        # The fake script was never even spawned.
        self.assertFalse(self.argv_capture.exists())


class CliAndWrapperTests(LiveFixtureCase):
    """--live wiring through manage.py and the sbp wrapper (subprocess)."""

    def run_cmd(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            cwd=ROOT,
            env=dict(os.environ),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    def test_manage_py_live_degrade_exits_zero(self) -> None:
        self.make_clean_clone("clean-repo")
        # SCRIPT_ENV points at a nonexistent script (setUp default).
        proc = self.run_cmd(
            [
                sys.executable,
                str(ENV_MANAGER_DIR / "manage.py"),
                "git-status",
                "--live",
                "--root",
                str(self.estate),
                "--depth",
                "2",
                "--format",
                "json",
            ]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["live"]["applied"])
        self.assertTrue(
            any(
                note.startswith("live comparison unavailable: ")
                for note in payload["notes"]
            )
        )

    def test_wrapper_passes_live_through(self) -> None:
        clean = self.make_clean_clone("clean-repo")
        self.install_fake_fleet(
            fleet_payload([repo_entry(os.path.realpath(clean), mismatch=True)])
        )
        proc = self.run_cmd(
            [
                "bash",
                str(ROOT / "scripts" / "sbp"),
                "git",
                "--live",
                "--root",
                str(self.estate),
                "--depth",
                "2",
                "--json",
            ]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["schema"], git_estate.SCHEMA)
        self.assertTrue(payload["live"]["applied"])
        rows = {Path(row["path"]).name: row for row in payload["repos"]}
        self.assertEqual(rows["clean-repo"]["origin_state"], "origin-newer")


if __name__ == "__main__":
    unittest.main()
