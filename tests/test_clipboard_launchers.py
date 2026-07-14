from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


class ClipboardLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.log"
        self._fake("clipboard-route-exec", "exit 0")
        self._fake("mosh", "exit 0")
        self._fake("ssh", "cat >/dev/null; printf 'devbox-1\\n'")
        self._fake("tmux", 'printf "%s\\n" "$*" >> "$TEST_LOG"')
        self.env = {
            **os.environ,
            # Keep launcher discovery hermetic: an operator installation under
            # ~/.local/bin must not make the missing-helper test pass.
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "TEST_LOG": str(self.log),
            "DEVL_TRANSPORT": "ssh",
        }
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fake(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)

    def _run(self, launcher: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT_DIR / "scripts" / "launchers" / launcher), *args],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_d3_outside_tmux_creates_disposable_local_boundary_and_exact_route(
        self,
    ) -> None:
        completed = self._run("d3")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        call = self.log.read_text()
        self.assertIn("new-session -s skillbox-d3-", call)
        self.assertIn("clipboard-route-exec", call)
        self.assertIn("--profile d3", call)
        self.assertIn("--transport ssh", call)
        self.assertIn("--target skillbox@skillbox-portfolio-devbox", call)

    def test_d2_records_the_allocated_remote_devbox_session(self) -> None:
        completed = self._run("d2")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        call = self.log.read_text()
        self.assertIn("new-session -s skillbox-d2-", call)
        self.assertIn("--profile devbox", call)
        self.assertIn("--remote-session devbox-1", call)

    def test_named_routes_choose_distinct_canonical_profiles(self) -> None:
        completed = self._run("d3", "sweet")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--profile sweet", self.log.read_text())

    def test_launcher_refuses_unregistered_route_ownership(self) -> None:
        (self.bin / "clipboard-route-exec").unlink()
        completed = self._run("d3")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("clipboard-route-exec is missing", completed.stderr)


if __name__ == "__main__":
    unittest.main()
