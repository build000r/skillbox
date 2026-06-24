"""Fixture-based tests for scripts/sbp_send_later.sh.

Drives the shell script via subprocess against a temp state dir, with `tmux`
and `crontab` stubbed on PATH so nothing touches the real box. Covers
scheduling, JSON contracts (list/doctor), the --when-waiting state machine,
bounded recurring jobs (--max-fires / --until), gc, bulk cancel, and the
regression guard for the swallowed-tmux_keys-send-failure bug.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = ROOT_DIR / "scripts" / "sbp_send_later.sh"

FAKE_TMUX = r"""#!/usr/bin/env bash
# Minimal tmux stub driven by env vars set by the test harness.
cmd="${1:-}"; shift || true
target=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  if [[ "${args[$i]}" == "-t" ]]; then target="${args[$((i+1))]}"; fi
done
norm() { printf '%s' "${1#=}"; }
case "$cmd" in
  display-message)
    t="$(norm "$target")"
    for live in ${FAKE_TMUX_LIVE//,/ }; do
      [[ "$t" == "$live" ]] && { echo "%1"; exit 0; }
    done
    exit 0  # not live -> empty stdout (caller checks for non-empty)
    ;;
  has-session)
    t="$(norm "$target")"
    for s in ${FAKE_TMUX_SESS//,/ }; do [[ "$t" == "$s" ]] && exit 0; done
    exit 1
    ;;
  capture-pane)
    cat "${FAKE_TMUX_CAPTURE:-/dev/null}" 2>/dev/null || true
    exit 0
    ;;
  send-keys)
    echo "send-keys target=$target args=$*" >> "${FAKE_TMUX_SENDLOG:-/dev/null}"
    t="$(norm "$target")"
    for dead in ${FAKE_TMUX_DEAD//,/ }; do [[ "$t" == "$dead" ]] && exit 1; done
    exit 0
    ;;
  list-panes)  exit 0 ;;
  list-windows) echo "0"; exit 0 ;;
  *) exit 0 ;;
esac
"""

FAKE_CRONTAB = r"""#!/usr/bin/env bash
# crontab stub: store/replay a crontab file under $FAKE_CRONTAB_STORE.
store="${FAKE_CRONTAB_STORE:?}"
if [[ "${1:-}" == "-l" ]]; then
  cat "$store" 2>/dev/null || exit 0
  exit 0
fi
if [[ -n "${1:-}" && -f "$1" ]]; then cp "$1" "$store"; exit 0; fi
exit 0
"""


class SendLaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = root / "state"
        self.logs = root / "logs"
        self.bin = root / "bin"
        self.bin.mkdir()
        self.state.mkdir()
        self.logs.mkdir()
        self.sendlog = root / "sendlog.txt"
        self.capture = root / "capture.txt"
        self.cronstore = root / "crontab.store"

        self._write_exec(self.bin / "tmux", FAKE_TMUX)
        self._write_exec(self.bin / "crontab", FAKE_CRONTAB)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- helpers ----------------------------------------------------------
    def _write_exec(self, path: Path, body: str) -> None:
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _run(self, *args: str, live: str = "", sess: str = "", dead: str = "",
            capture: str | None = None, check: bool = False):
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["SBP_SEND_LATER_STATE_DIR"] = str(self.state)
        env["SBP_SEND_LATER_LOG_DIR"] = str(self.logs)
        env["SBP_SEND_LATER_SBP_BIN"] = "/bin/true"
        env["FAKE_TMUX_LIVE"] = live
        env["FAKE_TMUX_SESS"] = sess
        env["FAKE_TMUX_DEAD"] = dead
        env["FAKE_TMUX_SENDLOG"] = str(self.sendlog)
        env["FAKE_CRONTAB_STORE"] = str(self.cronstore)
        if capture is not None:
            self.capture.write_text(capture)
            env["FAKE_TMUX_CAPTURE"] = str(self.capture)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True, text=True, env=env, check=check,
        )

    def _job_env(self, job_id: str) -> Path:
        return self.state / f"{job_id}.env"

    # -- tests ------------------------------------------------------------
    def test_help_lists_new_surfaces(self) -> None:
        r = self._run("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        for token in ("doctor", "gc", "fire ID", "--dry-run", "--max-fires",
                      "--until", "cancel --all"):
            self.assertIn(token, r.stdout, f"help missing {token!r}")

    def test_schedule_and_list_json(self) -> None:
        r = self._run("schedule", "--target", "sess:0.0", "--key", "hi",
                     "--key", "Enter", "--in", "5h", "--id", "j1", live="sess:0.0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self._job_env("j1").exists())

        r = self._run("list", "--json", live="sess:0.0")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["count"], 1)
        job = data["jobs"][0]
        self.assertEqual(job["id"], "j1")
        self.assertEqual(job["mode"], "tmux_keys")
        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["dest"], "sess:0.0")
        self.assertEqual(job["fires"], 0)
        # JSON contract: keys agents depend on must be present.
        for key in ("due_utc", "due_epoch", "recurring", "gate",
                    "last_fire_utc", "last_status", "overdue_s",
                    "max_fires", "until_utc"):
            self.assertIn(key, job)

    def test_dry_run_writes_nothing(self) -> None:
        r = self._run("schedule", "--target", "sess:0.0", "--key", "x",
                     "--in", "1h", "--id", "dryjob", "--dry-run", live="sess:0.0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assertFalse(self._job_env("dryjob").exists())

    def test_preflight_rejects_dead_target_without_force(self) -> None:
        r = self._run("schedule", "--target", "ghost:0.0", "--key", "x",
                     "--id", "g1", live="")  # not live
        self.assertEqual(r.returncode, 2)
        self.assertIn("not a live tmux pane", r.stderr)
        self.assertFalse(self._job_env("g1").exists())
        # --force bypasses the preflight.
        r = self._run("schedule", "--target", "ghost:0.0", "--key", "x",
                     "--id", "g1", "--force", live="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self._job_env("g1").exists())

    def test_fire_success_marks_done_and_sends(self) -> None:
        self._run("schedule", "--target", "live:0.0", "--key", "go",
                 "--key", "Enter", "--id", "f1", "--force")
        r = self._run("fire", "f1", live="live:0.0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.state / "f1.done").exists())
        sent = self.sendlog.read_text()
        self.assertIn("go", sent)

    def test_fire_send_failure_does_not_mark_done(self) -> None:
        """Regression guard (skillbox-prjm.2): a failed tmux send-keys must NOT
        be swallowed by the trailing sleep and mark a one-shot job done."""
        self._run("schedule", "--target", "dead:0.0", "--key", "go",
                 "--key", "Enter", "--id", "fail1", "--force")
        r = self._run("fire", "fail1", dead="dead:0.0", live="dead:0.0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.state / "fail1.done").exists(),
                         "job marked done despite send-keys failure")
        log = (self.state / "fail1.log").read_text()
        self.assertIn("send-keys failed", log)
        self.assertNotIn("status=0", log)

    def test_when_waiting_gate_requires_stable_idle(self) -> None:
        idle = "some output\n? for shortcuts\n"
        self._run("schedule", "--target", "g:0.0", "--key", "continue",
                 "--key", "Enter", "--id", "gate1", "--when-waiting",
                 "--recurring", "--force")
        # First fire: prev capture is empty so the screen "changed" -> running -> skip.
        r = self._run("fire", "gate1", live="g:0.0", capture=idle)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.sendlog.exists() and "continue" in self.sendlog.read_text())
        # Second fire: capture is byte-stable + idle footer -> waiting -> fire.
        r = self._run("fire", "gate1", live="g:0.0", capture=idle)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("continue", self.sendlog.read_text())

    def test_when_waiting_gate_skips_when_busy(self) -> None:
        busy = "thinking...\nesc to interrupt\n"
        self._run("schedule", "--target", "b:0.0", "--key", "x",
                 "--id", "gate2", "--when-waiting", "--recurring", "--force")
        self._run("fire", "gate2", live="b:0.0", capture=busy)
        self._run("fire", "gate2", live="b:0.0", capture=busy)
        self.assertFalse(self.sendlog.exists() and "x" in self.sendlog.read_text())

    def test_max_fires_stops_recurring_job(self) -> None:
        self._run("schedule", "--target", "m:0.0", "--key", "nudge",
                 "--id", "mx", "--recurring", "--max-fires", "2", "--force")
        for _ in range(4):
            self._run("fire", "mx", live="m:0.0")
        self.assertTrue((self.state / "mx.done").exists())
        done = (self.state / "mx.done").read_text()
        self.assertIn("reason=max_fires", done)
        self.assertEqual((self.state / "mx.fires").read_text().strip(), "2")
        sends = self.sendlog.read_text().count("nudge")
        self.assertEqual(sends, 2, f"expected 2 sends, got {sends}")

    def test_until_in_past_is_rejected(self) -> None:
        r = self._run("schedule", "--target", "u:0.0", "--key", "x",
                     "--id", "u1", "--until", "2000-01-01 00:00", live="u:0.0")
        self.assertEqual(r.returncode, 2)
        self.assertIn("past", r.stderr)

    def test_expired_job_marked_done(self) -> None:
        # Hand-craft a job whose deadline already passed, then fire it.
        self._run("schedule", "--target", "e:0.0", "--key", "x",
                 "--id", "exp", "--recurring", "--force")
        env_path = self._job_env("exp")
        text = env_path.read_text()
        text = text.replace("EXPIRE_EPOCH='0'", "EXPIRE_EPOCH='100'")
        env_path.write_text(text)
        self._run("fire", "exp", live="e:0.0")
        done = self.state / "exp.done"
        self.assertTrue(done.exists())
        self.assertIn("reason=expired", done.read_text())

    def test_gc_removes_orphans(self) -> None:
        (self.state / "orphan.log").write_text("stale\n")
        (self.state / "orphan.done").write_text("x\n")
        # A live job's files must survive.
        self._run("schedule", "--target", "k:0.0", "--key", "x",
                 "--id", "keep", "--force")
        r = self._run("gc")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.state / "orphan.log").exists())
        self.assertFalse((self.state / "orphan.done").exists())
        self.assertTrue(self._job_env("keep").exists())

    def test_cancel_all_and_purge_logs(self) -> None:
        self._run("schedule", "--target", "a:0.0", "--key", "x", "--id", "c1", "--force")
        self._run("schedule", "--target", "a:0.0", "--key", "y", "--id", "c2", "--force")
        (self.state / "c1.log").write_text("log\n")
        r = self._run("cancel", "--all", "--purge")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self._job_env("c1").exists())
        self.assertFalse(self._job_env("c2").exists())
        self.assertFalse((self.state / "c1.log").exists(), "--purge should remove logs")

    def test_cancel_match_glob(self) -> None:
        self._run("schedule", "--target", "a:0.0", "--key", "x", "--id", "keep-me", "--force")
        self._run("schedule", "--target", "a:0.0", "--key", "x", "--id", "tmp-1", "--force")
        self._run("schedule", "--target", "a:0.0", "--key", "x", "--id", "tmp-2", "--force")
        r = self._run("cancel", "--match", "tmp-*")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self._job_env("keep-me").exists())
        self.assertFalse(self._job_env("tmp-1").exists())
        self.assertFalse(self._job_env("tmp-2").exists())

    def test_doctor_json_contract(self) -> None:
        # Seed a healthy cron + fresh tick.
        self.cronstore.write_text(
            "* * * * * /bin/true send-later run-pending # sbp-send-later-wrapper\n")
        (self.logs / "ntm-send-later.cron.log").write_text("tick\n")
        self._run("schedule", "--target", "d:0.0", "--key", "x", "--id", "d1", "--force")
        r = self._run("doctor", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        for key in ("ok", "cron_installed", "tick_age_s", "tick_stale",
                    "jobs", "stale_locks", "orphans", "issues"):
            self.assertIn(key, data)
        self.assertTrue(data["cron_installed"])
        self.assertEqual(data["jobs"]["total"], 1)
        self.assertIsInstance(data["issues"], list)

    def test_run_pending_writes_heartbeat_and_doctor_reads_it(self) -> None:
        # Empty queue: run-pending writes nothing else, but must still heartbeat
        # so doctor can prove the tick is firing (not dead).
        self._run("run-pending")
        tick = self.state / ".last-tick"
        self.assertTrue(tick.exists(), "run-pending must write a heartbeat")
        self.cronstore.write_text(
            "* * * * * /bin/true send-later run-pending # sbp-send-later-wrapper\n")
        r = self._run("doctor", "--json")
        data = json.loads(r.stdout)
        self.assertFalse(data["tick_stale"], f"fresh heartbeat must read as not stale: {data}")
        self.assertGreaterEqual(data["tick_age_s"], 0)

    def test_doctor_flags_missing_cron(self) -> None:
        # Empty crontab store -> marker absent -> doctor must complain.
        self.cronstore.write_text("# nothing here\n")
        r = self._run("doctor", "--json")
        data = json.loads(r.stdout)
        self.assertFalse(data["cron_installed"])
        self.assertFalse(data["ok"])
        self.assertTrue(any("cron tick NOT installed" in i for i in data["issues"]))


if __name__ == "__main__":
    unittest.main()
