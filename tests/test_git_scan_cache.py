"""Tests for runtime_manager.git_scan_cache -- the ``sbp git`` last-scan cache.

Hermetic throughout: every cache read/write happens under a TemporaryDirectory
via ``SKILLBOX_STATE_ROOT``; wrapper-level tests subprocess the real
``scripts/sbp`` with ``--root`` pointed at a temp estate (pinned git config)
so the suite never scans the operator's ~/repos and never touches the real
state root. The home-view pin is explicit: the ambient git line must be
servable without spawning a single subprocess.
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

from runtime_manager import git_scan_cache  # noqa: E402

SBP = ROOT / "scripts" / "sbp"


def _envelope(**overrides) -> dict:
    """Minimal valid sbp-git/v1 envelope."""
    payload = {
        "schema": git_scan_cache.CACHE_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "roots": ["/tmp/estate"],
        "cwd_repo": None,
        "filters": [],
        "notes": [],
        "ignored_count": 0,
        "registry_applied": False,
        "repos": [],
        "summary": {},
        "registration_summary": {
            "registered": 0,
            "unregistered": 0,
            "unknown": 0,
            "stale_registered": 0,
        },
        "stale_registered": [],
        "repo_count": 0,
        "elapsed_seconds": 0.1,
    }
    payload.update(overrides)
    return payload


class StateRootCase(unittest.TestCase):
    """Temp state root exported as SKILLBOX_STATE_ROOT."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="git-scan-cache-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.state_root = self.tmp / "state"
        patcher = mock.patch.dict(
            os.environ, {"SKILLBOX_STATE_ROOT": str(self.state_root)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def cache_file(self) -> Path:
        return self.state_root / "git-scan" / "last-scan.json"

    def write_raw(self, text: str) -> Path:
        target = self.cache_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def write_valid(self, envelope: dict | None = None, *, age_seconds: float = 0) -> dict:
        envelope = envelope or _envelope()
        written = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        git_scan_cache.write_scan_cache(envelope, now=written)
        return envelope


class StateRootResolutionTests(StateRootCase):
    def test_env_var_wins(self) -> None:
        self.assertEqual(git_scan_cache.resolve_state_root(), self.state_root)
        self.assertEqual(
            git_scan_cache.cache_path(),
            self.state_root / "git-scan" / "last-scan.json",
        )

    def test_relative_env_value_resolves_against_cwd(self) -> None:
        with mock.patch.dict(os.environ, {"SKILLBOX_STATE_ROOT": ".skillbox-state"}):
            resolved = git_scan_cache.resolve_state_root()
        self.assertEqual(resolved, Path.cwd() / ".skillbox-state")

    def test_default_is_runtime_root_dot_skillbox_state(self) -> None:
        with mock.patch.dict(os.environ, {"SKILLBOX_STATE_ROOT": ""}):
            self.assertEqual(
                git_scan_cache.resolve_state_root(self.tmp),
                self.tmp / ".skillbox-state",
            )
            # No runtime_root -> the repo root this module lives in (the
            # Makefile's ./.skillbox-state default).
            self.assertEqual(
                git_scan_cache.resolve_state_root(), ROOT / ".skillbox-state"
            )


class WriteReadRoundTripTests(StateRootCase):
    def test_round_trip_returns_exact_envelope_and_age(self) -> None:
        envelope = _envelope(summary={"dirty": 2})
        target = git_scan_cache.write_scan_cache(envelope)
        self.assertEqual(target, self.cache_file())
        self.assertTrue(target.is_file())

        loaded = git_scan_cache.load_scan_cache()
        self.assertIsNotNone(loaded)
        got, age = loaded
        self.assertEqual(got, envelope)
        self.assertGreaterEqual(age, 0)
        self.assertLess(age, 60)

        # Atomic write leaves no temp residue beside the cache file.
        siblings = sorted(p.name for p in target.parent.iterdir())
        self.assertEqual(siblings, ["last-scan.json"])

    def test_failed_replace_leaves_previous_cache_intact(self) -> None:
        old = self.write_valid(_envelope(repo_count=1))
        with mock.patch.object(
            git_scan_cache.os, "replace", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                git_scan_cache.write_scan_cache(_envelope(repo_count=2))
        loaded = git_scan_cache.load_scan_cache()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded[0], old)
        siblings = sorted(p.name for p in self.cache_file().parent.iterdir())
        self.assertEqual(siblings, ["last-scan.json"], "tmp file not cleaned up")


class TwoGenerationRotationTests(StateRootCase):
    """Write rotation keeps EXACTLY one prior generation in the same file.

    ``load_scan_cache`` must keep returning the CURRENT generation exactly as
    before (the ``previous`` key is invisible to it -- structure_doctor's
    git_hygiene gate and ``--cached`` replay are pinned on that), while
    ``load_previous_scan`` serves the retained prior generation with the same
    validation contract.
    """

    def test_write_rotates_current_into_previous(self) -> None:
        first = _envelope(repo_count=1)
        second = _envelope(repo_count=2)
        git_scan_cache.write_scan_cache(first)
        git_scan_cache.write_scan_cache(second)

        stored = json.loads(self.cache_file().read_text(encoding="utf-8"))
        self.assertEqual(stored["envelope"], second)
        self.assertEqual(stored["previous"]["envelope"], first)
        self.assertIn("written_at", stored["previous"])

        current = git_scan_cache.load_scan_cache()
        self.assertIsNotNone(current)
        self.assertEqual(current[0], second)

        previous = git_scan_cache.load_previous_scan()
        self.assertIsNotNone(previous)
        self.assertEqual(previous[0], first)

    def test_third_write_drops_the_oldest_generation(self) -> None:
        for count in (1, 2, 3):
            git_scan_cache.write_scan_cache(_envelope(repo_count=count))
        stored = json.loads(self.cache_file().read_text(encoding="utf-8"))
        self.assertEqual(stored["envelope"]["repo_count"], 3)
        self.assertEqual(stored["previous"]["envelope"]["repo_count"], 2)
        self.assertNotIn("previous", stored["previous"], "only two generations ever")

    def test_previous_age_is_measured_from_its_own_written_at(self) -> None:
        first_written = datetime.now(timezone.utc) - timedelta(seconds=900)
        git_scan_cache.write_scan_cache(_envelope(repo_count=1), now=first_written)
        git_scan_cache.write_scan_cache(_envelope(repo_count=2))
        previous = git_scan_cache.load_previous_scan()
        self.assertIsNotNone(previous)
        self.assertAlmostEqual(previous[1], 900.0, delta=5)

    def test_legacy_single_generation_file_loads_with_no_previous(self) -> None:
        # Old writers stored only {written_at, envelope}: current loads fine,
        # previous is simply absent.
        legacy = _envelope(repo_count=7)
        self.write_raw(
            json.dumps(
                {
                    "written_at": datetime.now(timezone.utc).isoformat(),
                    "envelope": legacy,
                }
            )
        )
        loaded = git_scan_cache.load_scan_cache()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded[0], legacy)
        self.assertIsNone(git_scan_cache.load_previous_scan())

    def test_write_over_legacy_file_retains_it_as_previous(self) -> None:
        legacy = _envelope(repo_count=7)
        self.write_raw(
            json.dumps(
                {
                    "written_at": datetime.now(timezone.utc).isoformat(),
                    "envelope": legacy,
                }
            )
        )
        git_scan_cache.write_scan_cache(_envelope(repo_count=8))
        previous = git_scan_cache.load_previous_scan()
        self.assertIsNotNone(previous)
        self.assertEqual(previous[0], legacy)

    def test_invalid_previous_subtree_reads_absent_current_still_served(self) -> None:
        current = _envelope(repo_count=2)
        for bad_previous in (
            "not-a-dict",
            {"written_at": "yesterday-ish", "envelope": _envelope()},
            {"written_at": datetime.now(timezone.utc).isoformat(), "envelope": []},
            {
                "written_at": datetime.now(timezone.utc).isoformat(),
                "envelope": _envelope(schema="sbp-git/v2"),
            },
        ):
            with self.subTest(bad_previous=bad_previous):
                self.write_raw(
                    json.dumps(
                        {
                            "written_at": datetime.now(timezone.utc).isoformat(),
                            "envelope": current,
                            "previous": bad_previous,
                        }
                    )
                )
                self.assertIsNone(git_scan_cache.load_previous_scan())
                loaded = git_scan_cache.load_scan_cache()
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded[0], current)

    def test_write_over_corrupt_file_rotates_to_no_previous(self) -> None:
        self.write_raw("{torn json")
        git_scan_cache.write_scan_cache(_envelope(repo_count=1))
        stored = json.loads(self.cache_file().read_text(encoding="utf-8"))
        self.assertNotIn("previous", stored)
        self.assertIsNone(git_scan_cache.load_previous_scan())

    def test_missing_file_has_no_previous(self) -> None:
        self.assertIsNone(git_scan_cache.load_previous_scan())


class TtlTests(StateRootCase):
    def test_age_is_measured_from_written_at(self) -> None:
        written = datetime.now(timezone.utc) - timedelta(seconds=240)
        git_scan_cache.write_scan_cache(_envelope(), now=written)
        loaded = git_scan_cache.load_scan_cache(now=written + timedelta(seconds=240))
        self.assertIsNotNone(loaded)
        self.assertAlmostEqual(loaded[1], 240.0, delta=0.01)

    def test_future_written_at_clamps_to_zero(self) -> None:
        written = datetime.now(timezone.utc) + timedelta(seconds=3600)
        git_scan_cache.write_scan_cache(_envelope(), now=written)
        loaded = git_scan_cache.load_scan_cache()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded[1], 0.0)

    def test_ttl_boundary_fresh_at_exactly_ttl_stale_after(self) -> None:
        written = datetime.now(timezone.utc)
        envelope = _envelope(summary={"dirty": 1})
        git_scan_cache.write_scan_cache(envelope, now=written)

        at_ttl = written + timedelta(seconds=git_scan_cache.CACHE_TTL_SECONDS)
        self.assertEqual(
            git_scan_cache.home_view_line(now=at_ttl),
            f"git: 1 dirty ({git_scan_cache.CACHE_TTL_SECONDS // 60}m ago)",
        )
        past_ttl = at_ttl + timedelta(seconds=1)
        self.assertEqual(
            git_scan_cache.home_view_line(now=past_ttl),
            git_scan_cache.NO_RECENT_SCAN_HOME,
        )


class AbsentTreatmentTests(StateRootCase):
    """Every corrupt/missing shape reads as ABSENT: None, never an exception."""

    def assert_absent(self) -> None:
        self.assertIsNone(git_scan_cache.load_scan_cache())
        self.assertEqual(
            git_scan_cache.home_view_line(), git_scan_cache.NO_RECENT_SCAN_HOME
        )

    def test_missing_file(self) -> None:
        self.assert_absent()

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_unreadable_file(self) -> None:
        target = self.write_raw(json.dumps({"written_at": "x", "envelope": {}}))
        target.chmod(0o000)
        self.addCleanup(target.chmod, 0o600)
        self.assert_absent()

    def test_invalid_json(self) -> None:
        self.write_raw("{not json at all")
        self.assert_absent()

    def test_non_dict_payload(self) -> None:
        self.write_raw(json.dumps(["sbp-git/v1"]))
        self.assert_absent()

    def test_missing_envelope(self) -> None:
        self.write_raw(
            json.dumps({"written_at": datetime.now(timezone.utc).isoformat()})
        )
        self.assert_absent()

    def test_non_dict_envelope(self) -> None:
        self.write_raw(
            json.dumps(
                {
                    "written_at": datetime.now(timezone.utc).isoformat(),
                    "envelope": [1, 2],
                }
            )
        )
        self.assert_absent()

    def test_schema_version_mismatch_is_absent(self) -> None:
        """The pinned compatibility contract: a v2 envelope NEVER replays
        through v1 readers -- it reads as no cache at all."""
        self.write_raw(
            json.dumps(
                {
                    "written_at": datetime.now(timezone.utc).isoformat(),
                    "envelope": _envelope(schema="sbp-git/v2"),
                }
            )
        )
        self.assert_absent()

    def test_missing_written_at(self) -> None:
        self.write_raw(json.dumps({"envelope": _envelope()}))
        self.assert_absent()

    def test_unparseable_written_at(self) -> None:
        self.write_raw(
            json.dumps({"written_at": "yesterday-ish", "envelope": _envelope()})
        )
        self.assert_absent()


class SchemaPinTests(unittest.TestCase):
    def test_cache_schema_tracks_git_estate_schema(self) -> None:
        # git_scan_cache stays stdlib-only (no git_estate import), so pin the
        # constants against each other here instead.
        from runtime_manager import git_estate

        self.assertEqual(git_scan_cache.CACHE_SCHEMA, git_estate.SCHEMA)


class HomeViewLineTests(StateRootCase):
    def test_fresh_cache_renders_nonzero_counts_with_age(self) -> None:
        self.write_valid(
            _envelope(summary={"dirty": 7, "ahead-clean": 3, "mid-op": 1}),
            age_seconds=240,
        )
        self.assertEqual(
            git_scan_cache.home_view_line(), "git: 7 dirty, 3 ahead, 1 mid-op (4m ago)"
        )

    def test_diverged_and_unregistered_counts_surface(self) -> None:
        self.write_valid(
            _envelope(
                summary={"diverged-clean": 2},
                registration_summary={
                    "registered": 5,
                    "unregistered": 4,
                    "unknown": 0,
                    "stale_registered": 0,
                },
            ),
            age_seconds=30,
        )
        self.assertEqual(
            git_scan_cache.home_view_line(),
            "git: 2 diverged, 4 unregistered (30s ago)",
        )

    def test_all_clear_fresh_cache_says_clean_with_age(self) -> None:
        self.write_valid(_envelope(summary={"clean-current": 12}), age_seconds=61)
        self.assertEqual(git_scan_cache.home_view_line(), "git: clean (1m ago)")

    def test_stale_cache_points_at_sbp_git(self) -> None:
        self.write_valid(_envelope(summary={"dirty": 7}), age_seconds=7200)
        self.assertEqual(
            git_scan_cache.home_view_line(), git_scan_cache.NO_RECENT_SCAN_HOME
        )

    def test_home_view_never_spawns_a_subprocess(self) -> None:
        """The scan-from-home pin: with an EMPTY cache the ambient line must
        come back without any process spawn (no implicit rescan, ever)."""
        boom = AssertionError("home view spawned a subprocess")
        with mock.patch.object(subprocess, "Popen", side_effect=boom), mock.patch.object(
            os, "popen", side_effect=boom
        ), mock.patch.object(os, "system", side_effect=boom):
            self.assertEqual(
                git_scan_cache.home_view_line(), git_scan_cache.NO_RECENT_SCAN_HOME
            )
            self.write_valid(_envelope(summary={"dirty": 1}), age_seconds=5)
            self.assertEqual(git_scan_cache.home_view_line(), "git: 1 dirty (5s ago)")

    def test_module_import_never_pulls_the_scan_engine(self) -> None:
        """Belt-and-braces for the same pin: importing the cache module must
        not import git_estate/git_inventory (which own the git subprocesses)."""
        probe = (
            "import sys; sys.path.insert(0, '.env-manager'); "
            "import runtime_manager.git_scan_cache; "
            "bad = [m for m in sys.modules if 'git_estate' in m or 'git_inventory' in m]; "
            "assert not bad, bad"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class CliWriteThroughTests(StateRootCase):
    """The write-through seam lives in the git-status handler; git_estate
    stays pure. A cache write failure degrades to a stderr note and leaves
    stdout byte-identical."""

    def _args(self, **overrides) -> Namespace:
        base = dict(
            format="json", cwd=str(self.tmp), only=[], roots=[], depth=2, cached=False
        )
        base.update(overrides)
        return Namespace(**base)

    def _run_handler(self, cli, args) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli._handle_git_status(args, ROOT)
        return code, out.getvalue(), err.getvalue()

    def test_live_scan_write_throughs_the_exact_envelope(self) -> None:
        from runtime_manager import cli

        envelope = _envelope(summary={"dirty": 3})
        with mock.patch.object(cli, "git_estate_report", return_value=envelope):
            code, out, err = self._run_handler(cli, self._args())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), envelope)
        loaded = git_scan_cache.load_scan_cache()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded[0], envelope)

    def test_cache_write_failure_never_fails_the_scan(self) -> None:
        from runtime_manager import cli

        envelope = _envelope(summary={"dirty": 3})
        with mock.patch.object(
            cli, "git_estate_report", return_value=envelope
        ), mock.patch.object(
            cli, "write_git_scan_cache", side_effect=OSError("read-only fs")
        ):
            code, out, err = self._run_handler(cli, self._args())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), envelope, "stdout must be unaffected")
        self.assertIn("cache write failed", err)
        self.assertIsNone(git_scan_cache.load_scan_cache())

    def test_cached_flag_never_scans(self) -> None:
        from runtime_manager import cli

        self.write_valid(_envelope(repo_count=4), age_seconds=10)
        with mock.patch.object(
            cli, "git_estate_report", side_effect=AssertionError("--cached scanned")
        ):
            code, out, _ = self._run_handler(cli, self._args(cached=True))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["repo_count"], 4)
        self.assertAlmostEqual(payload["cache_age_seconds"], 10, delta=5)


class WrapperCacheTests(StateRootCase):
    """--cached and the home-view line through the real scripts/sbp wrapper."""

    def setUp(self) -> None:
        super().setUp()
        self.estate = self.tmp / "estate"
        self.estate.mkdir()
        self.config_root = self.tmp / "config"  # empty: registry degrades loudly
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text(
            "[user]\n\temail = fixture@example.invalid\n\tname = Cache Fixture\n"
            "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n",
            encoding="utf-8",
        )
        self.wrapper_env = {
            **os.environ,
            "SKILLBOX_ROOT": str(ROOT),
            "SKILLBOX_INVOKE_CWD": str(self.estate),
            "SKILLBOX_STATE_ROOT": str(self.state_root),
            "SKILLBOX_CONFIG_ROOT": str(self.config_root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_TERMINAL_PROMPT": "0",
        }

    def run_sbp(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SBP), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env=self.wrapper_env,
        )

    def make_dirty_repo(self, name: str) -> Path:
        repo = self.estate / name
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(repo)],
            check=True,
            env=self.wrapper_env,
            capture_output=True,
        )
        (repo / "loose.txt").write_text("loose\n", encoding="utf-8")
        return repo

    def scan_args(self) -> list[str]:
        return ["--root", str(self.estate), "--depth", "2"]

    def test_live_scan_write_through_then_cached_serves_verbatim(self) -> None:
        self.make_dirty_repo("a-dirty")

        live = self.run_sbp("git", "--json", *self.scan_args())
        self.assertEqual(live.returncode, 0, live.stderr)
        live_payload = json.loads(live.stdout)
        self.assertEqual(live_payload["schema"], "sbp-git/v1")

        stored = json.loads(self.cache_file().read_text(encoding="utf-8"))
        self.assertEqual(stored["envelope"], live_payload)
        self.assertIn("written_at", stored)

        cached = self.run_sbp("git", "--cached", "--json")
        self.assertEqual(cached.returncode, 0, cached.stderr)
        cached_payload = json.loads(cached.stdout)
        age = cached_payload.pop("cache_age_seconds")
        self.assertGreaterEqual(age, 0)
        self.assertLessEqual(age, git_scan_cache.CACHE_TTL_SECONDS)
        self.assertEqual(cached_payload, live_payload)

        cached_text = self.run_sbp("git", "--cached")
        self.assertEqual(cached_text.returncode, 0, cached_text.stderr)
        first_line = cached_text.stdout.splitlines()[0]
        self.assertTrue(first_line.startswith("cached "), first_line)
        self.assertIn("ago", first_line)
        self.assertIn("estate:", cached_text.stdout)

    def test_cached_stale_and_absent_exit_zero_with_pointer(self) -> None:
        # Absent cache.
        absent = self.run_sbp("git", "--cached")
        self.assertEqual(absent.returncode, 0, absent.stderr)
        self.assertIn("git: no recent scan — run sbp git", absent.stdout)

        absent_json = self.run_sbp("git", "--cached", "--json")
        self.assertEqual(absent_json.returncode, 0, absent_json.stderr)
        payload = json.loads(absent_json.stdout)
        self.assertEqual(payload["cached"], False)
        self.assertIn("no recent scan", payload["note"])

        # Stale cache: valid envelope, two hours old.
        self.write_valid(_envelope(summary={"dirty": 7}), age_seconds=7200)
        stale = self.run_sbp("git", "--cached")
        self.assertEqual(stale.returncode, 0, stale.stderr)
        self.assertIn("git: no recent scan — run sbp git", stale.stdout)

    def test_home_view_git_line_fresh_stale_absent(self) -> None:
        # Absent cache: home view still renders, with the pointer line, and
        # NEVER pays a scan for it.
        absent = self.run_sbp()
        self.assertEqual(absent.returncode, 0, absent.stderr)
        self.assertIn("git: no recent scan — sbp git", absent.stdout)
        self.assertFalse(self.cache_file().exists(), "home view must not scan/write")

        # Fresh cache: counts + age.
        self.write_valid(
            _envelope(summary={"dirty": 7, "ahead-clean": 3, "mid-op": 1}),
            age_seconds=240,
        )
        fresh = self.run_sbp()
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertIn("git: 7 dirty, 3 ahead, 1 mid-op (4m ago)", fresh.stdout)

        # Stale cache: back to the pointer.
        self.write_valid(_envelope(summary={"dirty": 7}), age_seconds=7200)
        stale = self.run_sbp()
        self.assertEqual(stale.returncode, 0, stale.stderr)
        self.assertIn("git: no recent scan — sbp git", stale.stdout)


if __name__ == "__main__":
    unittest.main()
