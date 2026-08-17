"""Real-PTY golden proofs that smart paste preserves baseline text semantics.

Every test here drives a *real* raw PTY behind an isolated ``tmux`` server on a
private socket, so the assertions are on bytes that an application actually
received -- not on a mocked ``subprocess.run``.

The harness in :class:`PtyPasteHarness` is shared by four groups:

``ClipboardPtyGoldenTests``
    The payload matrix (plain, multiline, shell metacharacters, file URLs,
    unicode, huge text, secret-bearing text) over a directly attached pane,
    plus the application-never-enabled-bracketed-paste row, which must degrade
    to unmarked text rather than injecting literal marker bytes.
``ClipboardPtyTransportTests``
    The same kitchen-sink payload re-proved through every transport reachable
    without leaving this machine: nested tmux, mosh over loopback, ssh over
    loopback, and ssh + remote tmux. Each transport is stood up privately
    (own tmux socket, own ``mosh-server``, own ``sshd`` with a throwaway host
    key on an ephemeral loopback port) and torn down in ``addCleanup``. No
    pre-existing tmux server, ssh host, or operator session is contacted.
``ClipboardPtyShellSafetyTests``
    Pasted text lands in a *real* interactive ``bash`` command line without an
    appended Return and without being evaluated.
``ClipboardPtyFallbackTests``
    Unsupported / native-owned / empty clipboard states run the full
    ``smart_paste`` orchestration against a real pane and must inject
    **nothing** -- no junk, no partial bracket, no stray marker. The
    unsupported-image row is re-proved behind nested tmux and behind
    ssh + remote tmux, each paired with its own control injection so an
    empty recording cannot be mistaken for a transport that never started.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from scripts.lib import clipboard_smart_paste as sp
from scripts.lib.clipboard_snapshot import ClipboardSnapshot


PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"


def remote_tmux_bin() -> str:
    """``tmux`` for an argv that leaves this process's PATH behind.

    A non-interactive ``ssh`` command runs with the login default PATH, which
    on macOS excludes Homebrew. A bare ``tmux`` in a remote argv is then "command
    not found" even though the class-level ``skipUnless`` proved tmux exists
    here, which silently turns the far-side-tmux row into a listener timeout
    instead of a paste-fidelity result.
    """
    return shutil.which("tmux") or "tmux"


# A raw-PTY listener. It enables bracketed-paste mode the way a real
# application does (argv[5]="0" makes it an application that never asked for
# bracketed paste), then records the exact bytes the terminal delivered.
# The recording is staged and ``os.replace``d so a reader can never observe a
# torn file.
CAPTURE_SOURCE = """
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
dwell_seconds = float(sys.argv[4])
bracketed = (sys.argv[5] if len(sys.argv) > 5 else "1") == "1"
fd = sys.stdin.fileno()
original = termios.tcgetattr(fd)
try:
    tty.setraw(fd)
    if bracketed:
        os.write(sys.stdout.fileno(), b"\\x1b[?2004h")
    ready.write_text("ready\\n", encoding="utf-8")
    deadline = time.monotonic() + dwell_seconds
    captured = bytearray()
    while len(captured) < expected_size and time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.05)
        if readable:
            chunk = os.read(fd, expected_size - len(captured))
            if not chunk:
                break
            captured.extend(chunk)
    staging = observed.with_name(observed.name + ".partial")
    staging.write_bytes(bytes(captured))
    os.replace(staging, observed)
finally:
    termios.tcsetattr(fd, termios.TCSANOW, original)
""".lstrip()

# Kitchen-sink payload: multiline, tab, command substitution, backticks,
# a pipeline, quotes, a backslash and a file URL -- everything the bead calls
# out, in one buffer, deliberately with no trailing newline.
TRANSPORT_PAYLOAD = (
    b"line one\n"
    b"line two\t$(id) `whoami` | rm -rf ~ ; echo 'q' \"q\" \\\\\n"
    b"file:///home/skillbox/Screen Shot.png"
)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def kill_tmux_socket(name: str) -> None:
    """Kill a private tmux server by socket name and unlink its socket file.

    Only ever called with socket names this module generated, so it can never
    reach the operator's default server or any pre-existing session.
    """
    subprocess.run(
        ["tmux", "-L", name, "kill-server"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    # A server whose pane command already exited leaves a dead socket file
    # behind, and it can no longer answer #{socket_path} -- so derive the path
    # the same way tmux does and unlink it unconditionally.
    base = os.environ.get("TMUX_TMPDIR") or "/tmp"
    try:
        os.unlink(Path(base) / f"tmux-{os.getuid()}" / name)
    except OSError:
        pass


def terminal_form(payload: bytes) -> bytes:
    """Bytes a terminal delivers for *payload*: LF becomes CR, nothing else."""
    return payload.replace(b"\n", b"\r")


def expected_stream(payload: bytes) -> bytes:
    return PASTE_START + terminal_form(payload) + PASTE_END


class IsolatedTmuxServer:
    """A tmux server on a private socket, killed and unlinked on cleanup.

    Nothing in this class can reach the operator's default server: every
    invocation carries ``-L <unique socket>`` and ``-f /dev/null``.
    """

    def __init__(self, test: unittest.TestCase, label: str = "pty") -> None:
        self.test = test
        self.socket = f"skillbox-clipboard-{label}-{uuid.uuid4().hex}"
        test.addCleanup(self.kill)

    @property
    def base(self) -> list[str]:
        return ["tmux", "-L", self.socket, "-f", "/dev/null"]

    def start_session(self, name: str, command: list[str]) -> str:
        subprocess.run(
            [*self.base, "new-session", "-d", "-s", name, "-x", "200", "-y", "50",
             *command],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return subprocess.run(
            ["tmux", "-L", self.socket, "display-message", "-p", "-t", name,
             "#{pane_id}"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()

    def capture_pane(self, pane: str) -> str:
        return subprocess.run(
            ["tmux", "-L", self.socket, "capture-pane", "-p", "-t", pane],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ).stdout

    def runner(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        """Re-target a ``tmux ...`` argv produced by the library at our socket."""
        assert command[0] == "tmux", command
        return subprocess.run(["tmux", "-L", self.socket, *command[1:]], **kwargs)

    def kill(self) -> None:
        kill_tmux_socket(self.socket)


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for PTY paste proof")
class PtyPasteHarness(unittest.TestCase):
    """Shared real-PTY plumbing. Subclasses only describe payloads/transports."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="skillbox-pty-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.capture_script = self.root / "capture.py"
        self.capture_script.write_text(CAPTURE_SOURCE, encoding="utf-8")
        self.ready = self.root / "ready"
        self.observed = self.root / "observed.bin"

    # -- listener plumbing -------------------------------------------------

    def capture_command(
        self, expected_size: int, dwell: float = 8.0, *, bracketed: bool = True
    ) -> list[str]:
        return [
            sys.executable,
            str(self.capture_script),
            str(self.ready),
            str(self.observed),
            str(expected_size),
            str(dwell),
            "1" if bracketed else "0",
        ]

    def wait_for(self, path: Path, timeout_seconds: float = 20.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(
            path.exists(), f"timed out waiting for isolated PTY: {path.name}"
        )

    def inject(self, server: IsolatedTmuxServer, pane: str, payload: bytes) -> None:
        sp.inject_bracketed_paste(
            pane=pane,
            data=payload,
            gesture_id="pty-golden",
            runner=server.runner,
        )

    def paste_through_pane(
        self,
        payload: bytes,
        *,
        wrap: object = None,
        label: str = "pty",
        dwell: float = 8.0,
        slack: int = 0,
        bracketed: bool = True,
    ) -> bytes:
        """Paste *payload* into a pane and return the bytes the PTY received.

        ``wrap`` optionally nests the listener behind a transport: it takes the
        listener argv and returns the argv tmux should run in the pane.
        ``bracketed=False`` models an application that never enabled DECSET
        2004, so tmux delivers the payload with no paste markers at all.
        """
        expected = expected_stream(payload) if bracketed else terminal_form(payload)
        expected_size = len(expected) + slack
        listener = self.capture_command(expected_size, dwell=dwell, bracketed=bracketed)
        pane_command = list(wrap(listener)) if wrap is not None else listener
        server = IsolatedTmuxServer(self, label)
        pane = server.start_session(label, pane_command)
        self.wait_for(self.ready)
        # Let the isolated tmux server consume the application's
        # bracketed-paste enable sequence before injecting the buffer.
        time.sleep(0.05)
        self.inject(server, pane, payload)
        self.wait_for(self.observed)
        return self.observed.read_bytes()

    def _start_private_sshd(self) -> list[str]:
        """Run a throwaway sshd on loopback and return an ssh argv for it.

        This never touches the operator's ``~/.ssh`` material, the system
        ``sshd``, or any remote host: the daemon has its own generated host
        key, its own ``authorized_keys``, and an ephemeral 127.0.0.1 port.
        """
        sshd = shutil.which("sshd") or (
            "/usr/sbin/sshd" if Path("/usr/sbin/sshd").exists() else None
        )
        if not (sshd and shutil.which("ssh") and shutil.which("ssh-keygen")):
            self.skipTest("sshd/ssh/ssh-keygen are required for the ssh row")
        lab = self.root / "sshd"
        lab.mkdir(mode=0o700, exist_ok=True)
        for name in ("hostkey", "userkey"):
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(lab / name)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        authorized = lab / "authorized_keys"
        authorized.write_bytes((lab / "userkey.pub").read_bytes())
        authorized.chmod(0o600)
        port = _free_loopback_port()
        config = lab / "sshd_config"
        config.write_text(
            "\n".join(
                [
                    f"Port {port}",
                    "ListenAddress 127.0.0.1",
                    f"HostKey {lab / 'hostkey'}",
                    f"AuthorizedKeysFile {authorized}",
                    "StrictModes no",
                    "UsePAM no",
                    "PasswordAuthentication no",
                    "KbdInteractiveAuthentication no",
                    "PermitUserEnvironment no",
                    f"PidFile {lab / 'sshd.pid'}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        started = subprocess.run(
            [sshd, "-f", str(config), "-E", str(lab / "sshd.log")],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        def stop_sshd() -> None:
            try:
                pid = int((lab / "sshd.pid").read_text().strip())
            except (OSError, ValueError):
                return
            try:
                os.kill(pid, 15)
            except (ProcessLookupError, PermissionError):
                pass

        self.addCleanup(stop_sshd)
        if started.returncode != 0:
            self.skipTest(
                "a private loopback sshd would not start: "
                f"{started.stderr.decode('utf-8', 'replace')[:200]!r}"
            )
        options = [
            "-p", str(port),
            "-i", str(lab / "userkey"),
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "IdentitiesOnly=yes",
            "-o", "LogLevel=ERROR",
            "-o", "ConnectTimeout=5",
        ]
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["ssh", *options, "127.0.0.1", "true"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if probe.returncode == 0:
                return ["ssh", "-tt", *options, "127.0.0.1"]
            time.sleep(0.2)
        self.skipTest("the private loopback sshd never accepted a connection")
        raise AssertionError("unreachable")

    def assert_verbatim(self, payload: bytes, captured: bytes) -> None:
        expected = expected_stream(payload)
        self.assertEqual(captured, expected)
        # Restated as independent invariants so a failure names the property.
        self.assertTrue(captured.startswith(PASTE_START))
        self.assertTrue(captured.endswith(PASTE_END))
        self.assertEqual(len(captured), len(payload) + 12)
        body = captured[len(PASTE_START) : -len(PASTE_END)]
        self.assertEqual(body, terminal_form(payload))
        self.assertNotIn(b"\n", body)  # CR semantics, never a raw LF
        # Nothing is appended after the end marker, and a payload that did not
        # end in a newline never grows a trailing Return.
        self.assertEqual(captured.split(PASTE_END)[-1], b"")
        if not payload.endswith(b"\n"):
            self.assertFalse(body.endswith(b"\r"))


class ClipboardPtyGoldenTests(PtyPasteHarness):
    """Payload matrix over a directly attached local tmux pane."""

    def test_actual_tmux_pty_preserves_bracketed_multiline_bytes_without_enter(
        self,
    ) -> None:
        payload = b"line one\nline two\t$() ' \\\"; no-final-newline"
        captured = self.paste_through_pane(payload, label="bracketed-paste")
        self.assert_verbatim(payload, captured)
        self.assertFalse(terminal_form(payload).endswith(b"\r"))
        # Nothing after the closing marker: the router appends no Return/Enter.
        self.assertEqual(captured.split(PASTE_END)[-1], b"")

    def test_plain_single_line_text_gets_markers_and_no_appended_return(self) -> None:
        payload = b"deploy the thing"
        captured = self.paste_through_pane(payload, label="plain")
        self.assert_verbatim(payload, captured)
        self.assertNotIn(b"\r", captured)

    def test_shell_metacharacters_and_quotes_survive_verbatim(self) -> None:
        payload = (
            b"$(touch /tmp/pwned) `id` ${HOME} && rm -rf / || true ; "
            b"cat <redirect >out 2>&1 | tee 'single' \"double\" \\escape #hash"
        )
        captured = self.paste_through_pane(payload, label="metachars")
        self.assert_verbatim(payload, captured)

    def test_multiline_snippet_uses_cr_semantics_for_every_line_break(self) -> None:
        payload = b"def f():\n    return 1\n\nprint(f())\n"
        captured = self.paste_through_pane(payload, label="multiline")
        self.assert_verbatim(payload, captured)
        self.assertEqual(captured.count(b"\r"), payload.count(b"\n"))

    def test_application_without_bracketed_paste_gets_text_and_no_marker_junk(
        self,
    ) -> None:
        # Many real paste targets never enable DECSET 2004 (plain ``cat``, a
        # shell with bracketed paste off, an editor in a raw mode). The router
        # must degrade to exactly what a manual ``tmux paste-buffer`` would
        # deliver: the payload in terminal form, with NO markers. If the router
        # ever hand-rolled ESC[200~ instead of leaving the decision to tmux's
        # ``-p``, this pane would receive literal escape junk it cannot parse.
        payload = TRANSPORT_PAYLOAD
        captured = self.paste_through_pane(
            payload, label="no-bracketed", bracketed=False
        )
        self.assertEqual(captured, terminal_form(payload))
        self.assertNotIn(PASTE_START, captured)
        self.assertNotIn(PASTE_END, captured)
        self.assertNotIn(b"\x1b[200", captured)
        self.assertNotIn(b"\x1b[201", captured)
        # Still no appended Return: a payload with no trailing newline must not
        # grow one just because the markers are absent.
        self.assertFalse(captured.endswith(b"\r"))
        self.assertEqual(captured.count(b"\r"), payload.count(b"\n"))
        self.assertNotIn(b"\n", captured)

    def test_file_urls_and_paths_with_spaces_are_not_quoted_or_escaped(self) -> None:
        payload = (
            b"file:///Users/rob/Desktop/Screen Shot 2026-07-25 at 10.31.02.png\n"
            b"/srv/skillbox/repos/opensource/skillbox/scripts/clipboard/hosts.json\n"
            b"~/Library/Application Support/thing.pdf"
        )
        captured = self.paste_through_pane(payload, label="fileurl")
        self.assert_verbatim(payload, captured)
        self.assertIn(b"Screen Shot 2026-07-25 at 10.31.02.png", captured)
        self.assertNotIn(b"\\ ", captured)  # no shell-style space escaping

    def test_unicode_payload_survives_as_utf8_bytes(self) -> None:
        payload = "curl — “smart quotes” — ✅ 日本語 — emoji 🚀".encode("utf-8")
        captured = self.paste_through_pane(payload, label="unicode")
        self.assert_verbatim(payload, captured)

    def test_payload_bytes_are_transmitted_without_rewriting_or_escaping(self) -> None:
        # Characterization: the router is a byte pipe. A payload that itself
        # contains an end-of-paste marker is delivered exactly as authored --
        # the router neither escapes it nor injects compensating junk. Any
        # application-side handling of that byte sequence is the terminal's
        # long-standing bracketed-paste contract, not a router behaviour.
        payload = b"before" + PASTE_END + b"after"
        captured = self.paste_through_pane(payload, label="embedded-marker")
        self.assertEqual(captured, PASTE_START + payload + PASTE_END)

    def test_huge_payload_is_delivered_whole_and_unmodified(self) -> None:
        payload = b"".join(
            b"line-%05d abcdefghijklmnopqrstuvwxyz 0123456789\n" % index
            for index in range(12_000)
        )
        self.assertGreater(len(payload), 512 * 1024)
        captured = self.paste_through_pane(payload, label="huge", dwell=20.0)
        self.assert_verbatim(payload, captured)
        self.assertEqual(
            hashlib.sha256(captured[6:-6]).hexdigest(),
            hashlib.sha256(terminal_form(payload)).hexdigest(),
        )

    def test_secret_bearing_text_pastes_verbatim_but_never_lands_in_a_receipt(
        self,
    ) -> None:
        secret = "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        payload = secret.encode()
        runtime = self.root / "runtime"
        server = IsolatedTmuxServer(self, "secret")
        pane = server.start_session(
            "secret", self.capture_command(len(expected_stream(payload)))
        )
        self.wait_for(self.ready)
        time.sleep(0.05)

        def capture_fn(**_kwargs: object) -> tuple[ClipboardSnapshot, str]:
            return (
                ClipboardSnapshot(
                    ok=True,
                    kind="text",
                    change_count=11,
                    byte_size=len(payload),
                    mime="text/plain;charset=utf-8",
                    sha256=hashlib.sha256(payload).hexdigest(),
                ),
                secret,
            )

        receipt = sp.smart_paste(
            pane=pane,
            client="/dev/pts/pty-golden",
            runtime_root=runtime,
            capture_fn=capture_fn,
            change_count_fn=lambda: 11,
            runner=server.runner,
            inject_text=True,
        )
        self.wait_for(self.observed)
        # The operator's own text is never mangled on its way to the terminal.
        self.assert_verbatim(payload, self.observed.read_bytes())
        self.assertEqual(receipt["outcome"], "text")
        self.assertEqual(receipt["injected"], {"kind": "text", "byte_size": len(payload)})
        # ...but the durable artifacts hold no plaintext secret.
        receipt_bytes = Path(receipt["receipt_path"]).read_bytes()
        self.assertNotIn(secret.encode(), receipt_bytes)
        self.assertNotIn(b"wJalrXUtnFEMI", receipt_bytes)
        for path in runtime.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), path)


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for PTY paste proof")
class ClipboardPtyTransportTests(PtyPasteHarness):
    """The same payload re-proved through each locally reachable transport."""

    def test_nested_tmux_preserves_bracketed_paste_end_to_end(self) -> None:
        inner_socket = f"skillbox-clipboard-inner-{uuid.uuid4().hex}"
        self.addCleanup(kill_tmux_socket, inner_socket)

        def wrap(listener: list[str]) -> list[str]:
            return [
                "tmux", "-L", inner_socket, "-f", "/dev/null",
                "new-session", "-s", "nested", "-x", "200", "-y", "50",
                *listener,
            ]

        captured = self.paste_through_pane(
            TRANSPORT_PAYLOAD, wrap=wrap, label="nested-outer", dwell=12.0
        )
        self.assert_verbatim(TRANSPORT_PAYLOAD, captured)

    def test_mosh_over_loopback_preserves_bracketed_paste_end_to_end(self) -> None:
        if not (shutil.which("mosh-server") and shutil.which("mosh-client")):
            self.skipTest("mosh-server/mosh-client are required for the mosh row")
        listener = self.capture_command(
            len(expected_stream(TRANSPORT_PAYLOAD)), dwell=12.0
        )
        port, key = self._start_mosh_server(listener)

        server = IsolatedTmuxServer(self, "mosh-client")
        pane = server.start_session(
            "mosh-client",
            [
                "env",
                f"MOSH_KEY={key}",
                "TERM=xterm-256color",
                "LC_ALL=en_US.UTF-8",
                "LANG=en_US.UTF-8",
                "mosh-client",
                "127.0.0.1",
                str(port),
            ],
        )
        self.wait_for(self.ready)
        time.sleep(0.3)  # mosh needs a round trip before the pty is live
        self.inject(server, pane, TRANSPORT_PAYLOAD)
        self.wait_for(self.observed)
        self.assert_verbatim(TRANSPORT_PAYLOAD, self.observed.read_bytes())

    def test_ssh_over_loopback_preserves_bracketed_paste_end_to_end(self) -> None:
        ssh_command = self._start_private_sshd()

        def wrap(listener: list[str]) -> list[str]:
            return [*ssh_command, shlex.join(listener)]

        captured = self.paste_through_pane(
            TRANSPORT_PAYLOAD, wrap=wrap, label="ssh", dwell=12.0
        )
        self.assert_verbatim(TRANSPORT_PAYLOAD, captured)

    def test_ssh_plus_remote_tmux_preserves_bracketed_paste_end_to_end(self) -> None:
        # "Remote" here means *on the far side of an ssh session*: a second
        # tmux server, started by the ssh command, whose pane the operator's
        # local tmux can only reach as an opaque byte stream. The loopback hop
        # proves the ssh channel and the far-side tmux do not rewrite paste
        # bytes; a genuinely cross-machine hop (different kernel, different
        # locale, different tmux build) is a separate, still-open row.
        ssh_command = self._start_private_sshd()
        remote_socket = f"skillbox-clipboard-remote-{uuid.uuid4().hex}"
        self.addCleanup(kill_tmux_socket, remote_socket)

        def wrap(listener: list[str]) -> list[str]:
            remote = [
                remote_tmux_bin(), "-L", remote_socket, "-f", "/dev/null",
                "new-session", "-s", "remote", "-x", "200", "-y", "50",
                *listener,
            ]
            return [*ssh_command, shlex.join(remote)]

        captured = self.paste_through_pane(
            TRANSPORT_PAYLOAD, wrap=wrap, label="ssh-remote-tmux", dwell=12.0
        )
        self.assert_verbatim(TRANSPORT_PAYLOAD, captured)

    # -- private transports ------------------------------------------------

    def _start_mosh_server(self, command: list[str]) -> tuple[int, str]:
        environment = {
            **os.environ,
            "LC_ALL": "en_US.UTF-8",
            "LANG": "en_US.UTF-8",
        }
        completed = subprocess.run(
            ["mosh-server", "new", "-i", "127.0.0.1", "-c", "256", "--", *command],
            check=False,
            text=True,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        connect = re.search(r"^MOSH CONNECT (\d+) (\S+)$", completed.stdout, re.M)
        detached = re.search(r"pid = (\d+)", completed.stdout)
        if completed.returncode != 0 or not connect:
            self.skipTest(f"mosh-server would not start locally: {completed.stdout!r}")
        if detached:
            pid = int(detached.group(1))

            def stop_mosh_server() -> None:
                try:
                    os.kill(pid, 15)
                except (ProcessLookupError, PermissionError):
                    pass

            self.addCleanup(stop_mosh_server)
        return int(connect.group(1)), connect.group(2)


@unittest.skipUnless(shutil.which("bash"), "bash is required for the no-eval proof")
class ClipboardPtyShellSafetyTests(PtyPasteHarness):
    """A real shell must receive the text, not run it."""

    def test_interactive_bash_never_executes_or_submits_pasted_text(self) -> None:
        sentinel = self.root / "SENTINEL"
        payload = (
            f"$(touch {sentinel}); echo pwned\nsecond line".encode()
        )
        server = IsolatedTmuxServer(self, "bash-safety")
        pane = server.start_session(
            "bash-safety",
            ["env", "PS1=LABPROMPT> ", "bash", "--norc", "--noprofile", "-i"],
        )
        time.sleep(1.0)
        self.inject(server, pane, payload)
        time.sleep(1.5)
        screen = server.capture_pane(pane)
        # The text is sitting on the command line...
        self.assertIn("$(touch", screen)
        self.assertIn("second line", screen)
        # ...and bash never ran it: no Return was appended, so no substitution
        # fired and `echo pwned` never produced output.
        self.assertFalse(sentinel.exists(), "pasted command substitution executed")
        self.assertNotIn("pwned\n", screen.replace("echo pwned", ""))


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for PTY paste proof")
class ClipboardPtyFallbackTests(PtyPasteHarness):
    """Unsupported/native clipboard states must inject nothing at all."""

    def _quiet_pane(
        self,
        *,
        wrap: object = None,
        label: str = "fallback",
        slot: str = "",
        dwell: float = 1.5,
    ) -> tuple[IsolatedTmuxServer, str]:
        # ``slot`` repoints the ready/observed files so one test can stand up
        # more than one pane (a transport row needs its own negative control).
        if slot:
            self.ready = self.root / f"ready-{slot}"
            self.observed = self.root / f"observed-{slot}.bin"
        # Expect nothing; dwell briefly and then record whatever arrived.
        listener = self.capture_command(4096, dwell=dwell)
        pane_command = list(wrap(listener)) if wrap is not None else listener
        server = IsolatedTmuxServer(self, label)
        pane = server.start_session(label, pane_command)
        self.wait_for(self.ready)
        time.sleep(0.05)
        return server, pane

    def _smart_paste(self, server: IsolatedTmuxServer, pane: str, capture_fn: object):
        return sp.smart_paste(
            pane=pane,
            client="/dev/pts/pty-golden",
            runtime_root=self.root / "runtime",
            capture_fn=capture_fn,
            change_count_fn=lambda: 5,
            runner=server.runner,
            inject_text=True,
        )

    def _assert_pty_saw_nothing(self) -> None:
        self.wait_for(self.observed)
        self.assertEqual(self.observed.read_bytes(), b"")

    def test_quiet_pane_control_does_observe_a_real_injection(self) -> None:
        # Negative control for every "injects nothing" assertion below: the
        # same listener, same dwell, same runner -- with real text it captures
        # the stream, so an empty recording really does mean "nothing sent".
        server, pane = self._quiet_pane()

        def capture_fn(**_kwargs: object) -> tuple[ClipboardSnapshot, str]:
            return (
                ClipboardSnapshot(
                    ok=True,
                    kind="text",
                    change_count=5,
                    byte_size=5,
                    mime="text/plain;charset=utf-8",
                    sha256=hashlib.sha256(b"junk?").hexdigest(),
                ),
                "junk?",
            )

        self._smart_paste(server, pane, capture_fn)
        self.wait_for(self.observed)
        self.assertEqual(self.observed.read_bytes(), expected_stream(b"junk?"))

    def test_unsupported_clipboard_state_injects_no_junk(self) -> None:
        server, pane = self._quiet_pane()

        def capture_fn(**_kwargs: object) -> tuple[ClipboardSnapshot, None]:
            return (
                ClipboardSnapshot(
                    ok=False,
                    kind="image",
                    change_count=5,
                    error={"code": "unsupported_type", "message": "heic without codec"},
                ),
                None,
            )

        with self.assertRaises(sp.SmartPasteError) as raised:
            self._smart_paste(server, pane, capture_fn)
        self.assertEqual(sp.error_code(raised.exception), "unsupported_type")
        self._assert_pty_saw_nothing()

    def test_finder_file_clipboard_stays_native_and_injects_nothing(self) -> None:
        server, pane = self._quiet_pane()

        def capture_fn(**_kwargs: object) -> tuple[ClipboardSnapshot, None]:
            return (
                ClipboardSnapshot(
                    ok=True,
                    kind="files",
                    change_count=5,
                    file_count=2,
                    file_names=("a.png", "b pdf.pdf"),
                    source_types=("public.file-url",),
                ),
                None,
            )

        receipt = self._smart_paste(server, pane, capture_fn)
        self.assertEqual(receipt["outcome"], "native_files")
        self.assertIsNone(receipt["injected"])
        self._assert_pty_saw_nothing()

    def test_empty_clipboard_is_a_silent_no_op_on_the_pty(self) -> None:
        server, pane = self._quiet_pane()

        def capture_fn(**_kwargs: object) -> tuple[ClipboardSnapshot, None]:
            return ClipboardSnapshot(ok=True, kind="empty", change_count=5), None

        receipt = self._smart_paste(server, pane, capture_fn)
        self.assertEqual(receipt["outcome"], "empty")
        self.assertIsNone(receipt["injected"])
        self._assert_pty_saw_nothing()

    def test_empty_text_payload_never_emits_bare_bracket_markers(self) -> None:
        server, pane = self._quiet_pane()

        def capture_fn(**_kwargs: object) -> tuple[ClipboardSnapshot, str]:
            return (
                ClipboardSnapshot(
                    ok=True,
                    kind="text",
                    change_count=5,
                    byte_size=0,
                    mime="text/plain;charset=utf-8",
                    sha256=hashlib.sha256(b"").hexdigest(),
                ),
                "",
            )

        receipt = self._smart_paste(server, pane, capture_fn)
        self.assertEqual(receipt["outcome"], "text")
        self._assert_pty_saw_nothing()

    # -- the same fallback, re-proved behind a transport --------------------

    @staticmethod
    def _unsupported_image_capture(
        **_kwargs: object,
    ) -> tuple[ClipboardSnapshot, None]:
        return (
            ClipboardSnapshot(
                ok=False,
                kind="image",
                change_count=5,
                error={"code": "unsupported_type", "message": "heic without codec"},
            ),
            None,
        )

    @staticmethod
    def _control_text_capture(**_kwargs: object) -> tuple[ClipboardSnapshot, str]:
        return (
            ClipboardSnapshot(
                ok=True,
                kind="text",
                change_count=5,
                byte_size=5,
                mime="text/plain;charset=utf-8",
                sha256=hashlib.sha256(b"junk?").hexdigest(),
            ),
            "junk?",
        )

    def _assert_transport_falls_back_without_junk(
        self, wrap: object, label: str
    ) -> None:
        """Prove a *live* transport-backed pane still receives zero bytes.

        The control run matters more than the assertion it guards: without it,
        a transport that failed to start would record an empty file and the
        "no junk" claim would pass for the wrong reason.
        """
        server, pane = self._quiet_pane(
            wrap=wrap, label=f"{label}-control", slot=f"{label}-control", dwell=6.0
        )
        self._smart_paste(server, pane, self._control_text_capture)
        self.wait_for(self.observed)
        self.assertEqual(
            self.observed.read_bytes(),
            expected_stream(b"junk?"),
            f"{label}: the control injection never reached the pane",
        )

        server, pane = self._quiet_pane(
            wrap=wrap, label=f"{label}-image", slot=f"{label}-image", dwell=6.0
        )
        with self.assertRaises(sp.SmartPasteError) as raised:
            self._smart_paste(server, pane, self._unsupported_image_capture)
        self.assertEqual(sp.error_code(raised.exception), "unsupported_type")
        self._assert_pty_saw_nothing()

    def test_unsupported_image_injects_no_junk_through_nested_tmux(self) -> None:
        def wrap(listener: list[str]) -> list[str]:
            inner_socket = f"skillbox-clipboard-fb-inner-{uuid.uuid4().hex}"
            self.addCleanup(kill_tmux_socket, inner_socket)
            return [
                "tmux", "-L", inner_socket, "-f", "/dev/null",
                "new-session", "-s", "nested-fallback", "-x", "200", "-y", "50",
                *listener,
            ]

        self._assert_transport_falls_back_without_junk(wrap, "nested-tmux")

    def test_unsupported_image_injects_no_junk_through_ssh_and_remote_tmux(
        self,
    ) -> None:
        ssh_command = self._start_private_sshd()

        def wrap(listener: list[str]) -> list[str]:
            remote_socket = f"skillbox-clipboard-fb-remote-{uuid.uuid4().hex}"
            self.addCleanup(kill_tmux_socket, remote_socket)
            remote = [
                remote_tmux_bin(), "-L", remote_socket, "-f", "/dev/null",
                "new-session", "-s", "remote-fallback", "-x", "200", "-y", "50",
                *listener,
            ]
            return [*ssh_command, shlex.join(remote)]

        self._assert_transport_falls_back_without_junk(wrap, "ssh-remote-tmux")


if __name__ == "__main__":
    unittest.main()
