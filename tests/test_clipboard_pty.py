from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from scripts.lib import clipboard_smart_paste as sp


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for PTY paste proof")
class ClipboardPtyGoldenTests(unittest.TestCase):
    def test_actual_tmux_pty_preserves_bracketed_multiline_bytes_without_enter(
        self,
    ) -> None:
        payload = b"line one\nline two\t$() ' \\\"; no-final-newline"
        terminal_payload = payload.replace(b"\n", b"\r")
        expected = b"\x1b[200~" + terminal_payload + b"\x1b[201~"
        socket = f"skillbox-clipboard-test-{uuid.uuid4().hex}"

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            capture_script = root / "capture.py"
            ready = root / "ready"
            observed = root / "observed.bin"
            capture_script.write_text(
                """
import os
import select
import sys
import termios
import time
import tty
from pathlib import Path

ready = Path(sys.argv[1])
observed = Path(sys.argv[2])
expected_size = int(sys.argv[3])
fd = sys.stdin.fileno()
original = termios.tcgetattr(fd)
try:
    tty.setraw(fd)
    os.write(sys.stdout.fileno(), b"\\x1b[?2004h")
    ready.write_text("ready\\n", encoding="utf-8")
    deadline = time.monotonic() + 3
    captured = bytearray()
    while len(captured) < expected_size and time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.1)
        if readable:
            captured.extend(os.read(fd, expected_size - len(captured)))
    observed.write_bytes(captured)
finally:
    termios.tcsetattr(fd, termios.TCSANOW, original)
""".lstrip(),
                encoding="utf-8",
            )

            base = ["tmux", "-L", socket, "-f", "/dev/null"]
            try:
                subprocess.run(
                    [
                        *base,
                        "new-session",
                        "-d",
                        "-s",
                        "bracketed-paste",
                        sys.executable,
                        str(capture_script),
                        str(ready),
                        str(observed),
                        str(len(expected)),
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                pane = subprocess.run(
                    [
                        *base,
                        "display-message",
                        "-p",
                        "-t",
                        "bracketed-paste",
                        "#{pane_id}",
                    ],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).stdout.strip()
                self._wait_for(ready)
                # Let the isolated tmux server consume the application's
                # bracketed-paste enable sequence before injecting the buffer.
                time.sleep(0.05)

                def isolated_tmux(
                    command: list[str], **kwargs: object
                ) -> subprocess.CompletedProcess[bytes]:
                    self.assertEqual(command[0], "tmux")
                    return subprocess.run(
                        ["tmux", "-L", socket, *command[1:]], **kwargs
                    )

                sp.inject_bracketed_paste(
                    pane=pane,
                    data=payload,
                    gesture_id="pty-golden",
                    runner=isolated_tmux,
                )
                self._wait_for(observed)
                captured = observed.read_bytes()
                self.assertEqual(captured, expected)
                self.assertFalse(terminal_payload.endswith(b"\r"))
            finally:
                subprocess.run(
                    ["tmux", "-L", socket, "kill-server"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )

    def _wait_for(self, path: Path, timeout_seconds: float = 3.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), f"timed out waiting for isolated PTY: {path.name}")


if __name__ == "__main__":
    unittest.main()
