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
SBP = ROOT_DIR / "scripts" / "sbp"
SAFE = ROOT_DIR / "scripts" / "sbp_safe.sh"


def _run_safe(
    *args: str,
    env: dict[str, str] | None = None,
    guard: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = {
        **os.environ,
        "SKILLBOX_ROOT": str(ROOT_DIR),
        "SKILLBOX_INVOKE_CWD": str(ROOT_DIR),
    }
    if env:
        merged.update(env)
    if guard is not None:
        merged["SBP_SAFE_GUARD"] = str(guard)
    return subprocess.run(
        [str(SAFE), *args],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=merged,
    )


def _fake_guard(tmpdir: Path, *, go: bool, load1: str = "1.0", cpu: str = "8") -> Path:
    path = tmpdir / "swarm-load-guard.sh"
    ceiling = "6.00" if go else "0.50"
    rec = "5" if go else "0"
    verdict_line = (
        f"[swarm-guard] GO: safe to launch up to {rec} worker(s)."
        if go
        else f"[swarm-guard] NO-GO: load {load1} exceeds ceiling {ceiling}."
    )
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            echo "[swarm-guard] load1={load1} cpu={cpu} abort_ceiling={ceiling} recommended_max_workers={rec}" >&2
            echo "{verdict_line}" >&2
            exit {0 if go else 1}
            """
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class SbpSafeTests(unittest.TestCase):
    def test_wrapper_dispatches_help(self) -> None:
        result = subprocess.run(
            [str(SBP), "safe", "--help"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "SKILLBOX_ROOT": str(ROOT_DIR),
                "SKILLBOX_INVOKE_CWD": str(ROOT_DIR),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sbp safe SECONDS", result.stdout)

    def test_capabilities_lists_safe(self) -> None:
        result = subprocess.run(
            [str(SBP), "capabilities", "--json"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "SKILLBOX_ROOT": str(ROOT_DIR),
                "SKILLBOX_INVOKE_CWD": str(ROOT_DIR),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = {c["name"]: c for c in json.loads(result.stdout)["commands"]}
        self.assertIn("safe", commands)
        self.assertEqual(commands["safe"]["safe_first_try"], "sbp safe --json")

    def test_once_go_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            guard = _fake_guard(Path(tmp), go=True, load1="2.5")
            result = _run_safe("--json", "--factor", "0.75", guard=guard)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["verdict"], "GO")
        self.assertEqual(payload["load1"], "2.5")
        self.assertEqual(payload["recommended_max_workers"], "5")

    def test_once_nogo_human(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            guard = _fake_guard(Path(tmp), go=False, load1="9.9")
            result = _run_safe(guard=guard)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("NO-GO", result.stdout)
        self.assertIn("load1=9.9", result.stdout)

    def test_watch_count_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            guard = _fake_guard(Path(tmp), go=True)
            result = _run_safe("1", "--count", "2", "--json", guard=guard)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertEqual(json.loads(line)["verdict"], "GO")

    def test_bad_interval(self) -> None:
        result = _run_safe("nope")
        self.assertEqual(result.returncode, 2)
        self.assertIn("interval must be seconds", result.stderr)


if __name__ == "__main__":
    unittest.main()
