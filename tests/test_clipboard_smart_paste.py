from __future__ import annotations

import contextlib
import hashlib
import io
import json
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts.lib import clipboard_session as cs
from scripts.lib import clipboard_smart_paste as sp
from scripts.lib.clipboard_snapshot import ClipboardSnapshot
from scripts.lib.clipboard_transfer import TransferError


ROOT_DIR = Path(__file__).resolve().parents[1]
HOSTS = ROOT_DIR / "scripts" / "clipboard" / "hosts.json"


class FakeTmuxBytes:
    def __init__(self) -> None:
        self.options: dict[tuple[str, str], str] = {}
        self.buffers: dict[str, bytes] = {}
        self.injections: list[tuple[str, bytes, bool]] = []
        self.notices: list[str] = []
        self.fail_paste = False

    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        args = command[1:]
        if args[0] == "show-option":
            pane = args[args.index("-t") + 1]
            option = args[-1]
            value = self.options.get((pane, option), "")
            return subprocess.CompletedProcess(command, 0, (value + "\n").encode(), b"")
        if args[0] == "load-buffer":
            name = args[args.index("-b") + 1]
            self.buffers[name] = kwargs.get("input") or b""
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if args[0] == "paste-buffer":
            if self.fail_paste:
                return subprocess.CompletedProcess(command, 1, b"", b"paste failed")
            name = args[args.index("-b") + 1]
            pane = args[args.index("-t") + 1]
            self.injections.append((pane, self.buffers[name], "-p" in args))
            if "-d" in args:
                self.buffers.pop(name, None)
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if args[0] == "delete-buffer":
            name = args[args.index("-b") + 1]
            self.buffers.pop(name, None)
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if args[0] == "display-message":
            self.notices.append(args[-1])
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return subprocess.CompletedProcess(command, 1, b"", b"unexpected command")


class FakeTmuxText:
    def __call__(
        self, command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "", "")


class ClipboardSmartPasteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "runtime"
        self.routes = self.root / "routes"
        self.tmux = FakeTmuxBytes()
        self.pane = "%1"
        self.client = "/dev/ttys001"
        record, self.route_path = cs.register(
            profile="d3",
            transport="mosh",
            tmux_pane=self.pane,
            tmux_client=self.client,
            tmux_server="default",
            remote_session="devbox-1",
            root=self.routes,
            hosts_path=HOSTS,
            now=1_000.0,
            ttl_seconds=10_000_000_000,
            stamp_tmux=False,
        )
        self.generation = str(record["generation"])
        self.tmux.options[(self.pane, cs.TMUX_ROUTE_OPTION)] = str(self.route_path)
        self.tmux.options[(self.pane, cs.TMUX_GENERATION_OPTION)] = self.generation
        self.clock = iter((2_000.0, 2_000.1))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def smart(self, *, capture_fn: object, **overrides: object) -> dict[str, object]:
        args: dict[str, object] = {
            "pane": self.pane,
            "client": self.client,
            "route_path": self.route_path,
            "generation": self.generation,
            "runtime_root": self.runtime,
            "now": lambda: next(self.clock),
            "capture_fn": capture_fn,
            "change_count_fn": lambda: 7,
            "runner": self.tmux,
        }
        args.update(overrides)
        return sp.smart_paste(**args)

    @staticmethod
    def text_capture(text: str, change_count: int = 7) -> object:
        data = text.encode()

        def capture_fn(**_kwargs: object) -> tuple[ClipboardSnapshot, str]:
            return (
                ClipboardSnapshot(
                    ok=True,
                    kind="text",
                    change_count=change_count,
                    byte_size=len(data),
                    mime="text/plain;charset=utf-8",
                    sha256=hashlib.sha256(data).hexdigest(),
                ),
                text,
            )

        return capture_fn

    def image_capture(self, change_count: int = 7) -> object:
        image = self.root / "snapshot.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        image.chmod(0o600)
        digest = hashlib.sha256(image.read_bytes()).hexdigest()

        def capture_fn(**_kwargs: object) -> tuple[ClipboardSnapshot, None]:
            return (
                ClipboardSnapshot(
                    ok=True,
                    kind="image",
                    change_count=change_count,
                    byte_size=image.stat().st_size,
                    mime="image/png",
                    sha256=digest,
                    artifact=str(image),
                ),
                None,
            )

        return capture_fn

    def test_text_stays_on_ghostty_native_paste_path(self) -> None:
        text = "line one\nline two\t$() ' \""
        receipt = self.smart(capture_fn=self.text_capture(text))
        self.assertEqual(self.tmux.injections, [])
        self.assertEqual(receipt["outcome"], "native_text")
        self.assertIsNone(receipt["injected"])
        self.assertFalse(self.tmux.buffers)

    def test_ctrl_v_path_injects_text_once_as_bracketed_paste(self) -> None:
        text = "line one\nline two\t$() ' \""
        receipt = self.smart(capture_fn=self.text_capture(text), inject_text=True)
        self.assertEqual(self.tmux.injections, [(self.pane, text.encode(), True)])
        self.assertEqual(receipt["outcome"], "text")
        self.assertEqual(receipt["injected"]["byte_size"], len(text.encode()))
        self.assertFalse(self.tmux.buffers)

    def test_image_transfers_then_injects_remote_path_for_codex(self) -> None:
        transfers: list[tuple[Path, str]] = []

        def transfer(
            path: Path, *, ssh_target: str, **_kwargs: object
        ) -> dict[str, object]:
            transfers.append((path, ssh_target))
            return {
                "path": "/home/skillbox/.cache/skillbox/paste-artifacts/abc.png",
                "sha256": "a" * 64,
                "byte_size": path.stat().st_size,
                "mode": "0600",
                "reused": False,
            }

        receipt = self.smart(capture_fn=self.image_capture(), transfer_fn=transfer)
        self.assertEqual(transfers[0][1], "skillbox@skillbox-portfolio-devbox")
        self.assertEqual(
            self.tmux.injections,
            [
                (
                    self.pane,
                    b"/home/skillbox/.cache/skillbox/paste-artifacts/abc.png",
                    True,
                )
            ],
        )
        self.assertEqual(receipt["outcome"], "image_path")
        self.assertFalse((self.root / "snapshot.png").exists())

    def test_slow_transfer_progress_is_delayed_and_cancelled_after_completion(
        self,
    ) -> None:
        timers: list[object] = []

        class FakeTimer:
            daemon = False

            def __init__(self, delay: float, *_args: object, **_kwargs: object) -> None:
                self.delay = delay
                self.started = False
                self.cancelled = False
                timers.append(self)

            def start(self) -> None:
                self.started = True

            def cancel(self) -> None:
                self.cancelled = True

        with mock.patch.object(sp.threading, "Timer", FakeTimer):
            self.smart(
                capture_fn=self.image_capture(),
                transfer_fn=lambda *_args, **_kwargs: {
                    "path": "/remote/image.png",
                    "sha256": "a" * 64,
                    "reused": False,
                },
            )
        timer = timers[0]
        self.assertEqual(timer.delay, sp.PROGRESS_DELAY_SECONDS)  # type: ignore[attr-defined]
        self.assertTrue(timer.started)  # type: ignore[attr-defined]
        self.assertTrue(timer.cancelled)  # type: ignore[attr-defined]

    def test_local_image_path_needs_no_network(self) -> None:
        called = False

        def transfer(**_kwargs: object) -> dict[str, object]:
            nonlocal called
            called = True
            return {}

        receipt = self.smart(
            capture_fn=self.image_capture(),
            route_path=None,
            generation=None,
            transfer_fn=transfer,
        )
        self.assertFalse(called)
        self.assertTrue(self.tmux.injections[0][1].endswith(b"snapshot.png"))
        self.assertEqual(receipt["route_id"], None)
        self.assertTrue((self.root / "snapshot.png").exists())

    def test_clipboard_change_after_transfer_prevents_injection(self) -> None:
        transferred = False
        cleaned: list[str] = []

        def transfer(*_args: object, **_kwargs: object) -> dict[str, object]:
            nonlocal transferred
            transferred = True
            return {
                "path": "/remote/image.png",
                "sha256": "a" * 64,
                "reused": False,
            }

        with self.assertRaisesRegex(sp.SmartPasteError, "clipboard changed"):
            self.smart(
                capture_fn=self.image_capture(),
                transfer_fn=transfer,
                change_count_fn=lambda: 8,
                cleanup_fn=lambda **kwargs: cleaned.append(kwargs["sha256"]) or {},
            )
        self.assertTrue(transferred)
        self.assertEqual(cleaned, ["a" * 64])
        self.assertFalse(self.tmux.injections)

    def test_route_generation_change_after_transfer_prevents_injection(self) -> None:
        cleaned: list[str] = []

        def transfer(*_args: object, **_kwargs: object) -> dict[str, object]:
            self.tmux.options[(self.pane, cs.TMUX_GENERATION_OPTION)] = "replaced"
            return {
                "path": "/remote/image.png",
                "sha256": "b" * 64,
                "reused": False,
            }

        with self.assertRaisesRegex(sp.SmartPasteError, "route changed"):
            self.smart(
                capture_fn=self.image_capture(),
                transfer_fn=transfer,
                cleanup_fn=lambda **kwargs: cleaned.append(kwargs["sha256"]) or {},
            )
        self.assertEqual(cleaned, ["b" * 64])
        self.assertFalse(self.tmux.injections)

    def test_newer_gesture_supersedes_old_before_injection(self) -> None:
        base_capture = self.image_capture()

        def superseding_capture(**kwargs: object) -> tuple[ClipboardSnapshot, None]:
            result = base_capture(**kwargs)
            generation_files = list(self.runtime.glob("pane-*.generation"))
            self.assertEqual(len(generation_files), 1)
            generation_files[0].write_text("newer\n")
            return result

        with self.assertRaisesRegex(sp.SmartPasteError, "newer paste"):
            self.smart(
                capture_fn=superseding_capture,
                transfer_fn=lambda *_args, **_kwargs: {
                    "path": "/home/skillbox/.cache/skillbox/paste-artifacts/old.png"
                },
            )
        self.assertFalse(self.tmux.injections)

    def test_concurrent_panes_cannot_cross_deliver_remote_paths(self) -> None:
        second_pane = "%2"
        second_record, second_route = cs.register(
            profile="d3",
            transport="mosh",
            tmux_pane=second_pane,
            tmux_client=self.client,
            tmux_server="default",
            root=self.routes,
            hosts_path=HOSTS,
            now=1_000.0,
            ttl_seconds=10_000_000_000,
            stamp_tmux=False,
        )
        second_generation = str(second_record["generation"])
        self.tmux.options[(second_pane, cs.TMUX_ROUTE_OPTION)] = str(second_route)
        self.tmux.options[(second_pane, cs.TMUX_GENERATION_OPTION)] = (
            second_generation
        )
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def capture_for(name: str) -> object:
            path = self.root / f"{name}.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
            digest = hashlib.sha256(path.read_bytes()).hexdigest()

            def capture_fn(**_kwargs: object) -> tuple[ClipboardSnapshot, None]:
                return (
                    ClipboardSnapshot(
                        ok=True,
                        kind="image",
                        change_count=7,
                        byte_size=path.stat().st_size,
                        mime="image/png",
                        sha256=digest,
                        artifact=str(path),
                    ),
                    None,
                )

            return capture_fn

        def transfer(path: Path, **_kwargs: object) -> dict[str, object]:
            barrier.wait(timeout=2)
            name = path.stem
            return {
                "path": f"/home/skillbox/.cache/skillbox/paste-artifacts/{name}.png",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "reused": False,
            }

        def run(
            pane: str,
            route_path: Path,
            generation: str,
            capture_fn: object,
        ) -> None:
            try:
                sp.smart_paste(
                    pane=pane,
                    client=self.client,
                    route_path=route_path,
                    generation=generation,
                    runtime_root=self.runtime,
                    now=time.time,
                    capture_fn=capture_fn,  # type: ignore[arg-type]
                    change_count_fn=lambda: 7,
                    transfer_fn=transfer,
                    runner=self.tmux,
                )
            except BaseException as exc:  # test captures worker failures
                errors.append(exc)

        threads = [
            threading.Thread(
                target=run,
                args=(self.pane, self.route_path, self.generation, capture_for("first")),
            ),
            threading.Thread(
                target=run,
                args=(second_pane, second_route, second_generation, capture_for("second")),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(errors)
        self.assertEqual(
            set(self.tmux.injections),
            {
                (
                    self.pane,
                    b"/home/skillbox/.cache/skillbox/paste-artifacts/first.png",
                    True,
                ),
                (
                    second_pane,
                    b"/home/skillbox/.cache/skillbox/paste-artifacts/second.png",
                    True,
                ),
            },
        )
        self.assertFalse((self.root / "first.png").exists())
        self.assertFalse((self.root / "second.png").exists())

    def test_transfer_failure_prevents_injection(self) -> None:
        def transfer(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise TransferError("offline")

        with self.assertRaisesRegex(TransferError, "offline"):
            self.smart(capture_fn=self.image_capture(), transfer_fn=transfer)
        self.assertFalse(self.tmux.injections)
        self.assertFalse((self.root / "snapshot.png").exists())

    def test_expected_proof_digest_mismatch_prevents_transfer_and_cleans_snapshot(
        self,
    ) -> None:
        called = False

        def transfer(*_args: object, **_kwargs: object) -> dict[str, object]:
            nonlocal called
            called = True
            return {}

        with self.assertRaisesRegex(sp.SmartPasteError, "expected proof artifact"):
            self.smart(
                capture_fn=self.image_capture(),
                transfer_fn=transfer,
                expected_sha256="0" * 64,
            )
        self.assertFalse(called)
        self.assertFalse((self.root / "snapshot.png").exists())

    def test_unsupported_route_fails_before_transfer(self) -> None:
        fallback, path = cs.register(
            profile="conference1-fallback",
            transport="wsl",
            tmux_pane="%2",
            tmux_client=self.client,
            root=self.routes,
            hosts_path=HOSTS,
            now=1_000.0,
            ttl_seconds=10_000_000_000,
            stamp_tmux=False,
        )
        generation = str(fallback["generation"])
        self.tmux.options[("%2", cs.TMUX_ROUTE_OPTION)] = str(path)
        self.tmux.options[("%2", cs.TMUX_GENERATION_OPTION)] = generation
        with self.assertRaisesRegex(sp.SmartPasteError, "does not support"):
            sp.smart_paste(
                pane="%2",
                client=self.client,
                route_path=path,
                generation=generation,
                runtime_root=self.runtime,
                now=lambda: 2_000.0,
                capture_fn=self.image_capture(),
                change_count_fn=lambda: 7,
                runner=self.tmux,
            )

    def test_empty_clipboard_is_quiet_noop_with_receipt(self) -> None:
        def capture_fn(**_kwargs: object) -> tuple[ClipboardSnapshot, None]:
            return ClipboardSnapshot(ok=True, kind="empty", change_count=7), None

        receipt = self.smart(capture_fn=capture_fn)
        self.assertEqual(receipt["outcome"], "empty")
        self.assertFalse(self.tmux.injections)

    def test_finder_multi_item_clipboard_stays_on_native_safe_fallback(self) -> None:
        def capture_fn(**_kwargs: object) -> tuple[ClipboardSnapshot, None]:
            return (
                ClipboardSnapshot(
                    ok=True,
                    kind="files",
                    change_count=7,
                    file_count=2,
                    file_names=("first.png", "second.pdf"),
                    _source_paths=("/Users/test/first.png", "/Users/test/second.pdf"),
                ),
                None,
            )

        receipt = self.smart(capture_fn=capture_fn)
        self.assertEqual(receipt["outcome"], "native_files")
        self.assertFalse(self.tmux.injections)
        self.assertIsNone(receipt["transfer"])
        serialized = json.dumps(receipt)
        self.assertNotIn("first.png", serialized)
        self.assertNotIn("second.pdf", serialized)

    def test_pdf_is_transferred_and_injected_as_document_reference(self) -> None:
        document = self.root / "fixture.pdf"
        document.write_bytes(b"%PDF-1.7\nfixture")
        document.chmod(0o600)
        digest = hashlib.sha256(document.read_bytes()).hexdigest()

        def capture_fn(**_kwargs: object) -> tuple[ClipboardSnapshot, None]:
            return (
                ClipboardSnapshot(
                    ok=True,
                    kind="document",
                    change_count=7,
                    byte_size=document.stat().st_size,
                    mime="application/pdf",
                    sha256=digest,
                    artifact=str(document),
                ),
                None,
            )

        receipt = self.smart(
            capture_fn=capture_fn,
            transfer_fn=lambda *_args, **_kwargs: {
                "path": "/home/skillbox/.cache/skillbox/paste-artifacts/fixture.pdf"
            },
        )
        self.assertEqual(receipt["outcome"], "document_path")
        self.assertEqual(
            self.tmux.injections[0][1],
            b"/home/skillbox/.cache/skillbox/paste-artifacts/fixture.pdf",
        )

    def test_receipt_is_private_and_redacts_text(self) -> None:
        secret = "do-not-log-this-text"
        receipt = self.smart(capture_fn=self.text_capture(secret))
        path = Path(str(receipt["receipt_path"]))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn(secret, path.read_text())
        self.assertIn('"outcome":"native_text"', path.read_text())

    def test_paste_failure_deletes_temporary_tmux_buffer(self) -> None:
        self.tmux.fail_paste = True
        with self.assertRaisesRegex(sp.SmartPasteError, "paste failed"):
            self.smart(
                capture_fn=self.image_capture(),
                transfer_fn=lambda *_args, **_kwargs: {
                    "path": "/home/skillbox/.cache/skillbox/paste-artifacts/fail.png"
                },
            )
        self.assertFalse(self.tmux.buffers)

    def test_error_codes_are_stable_for_every_operator_failure_class(self) -> None:
        cases = {
            "artifact transfer timed out after 3s": "target_offline",
            "authentication failed": "auth_failed",
            "artifact exceeds 20 bytes": "artifact_too_large",
            "clipboard changed during image transfer": "clipboard_changed",
            "route changed before paste completed": "stale_route",
            "a newer paste superseded this gesture": "focus_changed",
            "registered route does not support smart image paste": "unsupported_type",
            "pane paste failed": "injection_failed",
            "remote cleanup also failed": "cleanup_failed",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(sp.error_code(RuntimeError(message)), expected)

    def test_cli_failure_writes_private_bounded_receipt_and_one_repair(self) -> None:
        output = io.StringIO()
        notices: list[str] = []
        secret_path = "/Users/operator/Secret Project/private-image.png"
        with (
            mock.patch.object(
                sp,
                "smart_paste",
                side_effect=TransferError(f"authentication failed for {secret_path}"),
            ),
            mock.patch.object(
                sp,
                "notify",
                side_effect=lambda message, **_kwargs: notices.append(message),
            ),
            contextlib.redirect_stdout(output),
        ):
            code = sp.main(
                [
                    "--pane",
                    self.pane,
                    "--client",
                    self.client,
                    "--runtime-root",
                    str(self.runtime),
                    "--json",
                ]
            )
        self.assertEqual(code, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["error"]["code"], "auth_failed")
        self.assertNotIn(secret_path, output.getvalue())
        self.assertNotIn(secret_path, notices[0])
        self.assertEqual(
            payload["repair"],
            "press Cmd+V to retry or run clipboard-paste doctor",
        )
        receipt = Path(payload["receipt_path"])
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        self.assertIn(secret_path, receipt.read_text())
        self.assertEqual(len(notices), 1)
        self.assertLessEqual(len(notices[0]), 400)

    def test_cli_failure_with_unsafe_runtime_still_returns_redacted_json(self) -> None:
        unsafe_runtime = self.root / "unsafe-runtime"
        unsafe_runtime.symlink_to(self.root / "redirected-runtime")
        output = io.StringIO()
        notices: list[str] = []
        secret_path = "/Users/operator/private-image.png"
        with (
            mock.patch.object(
                sp,
                "smart_paste",
                side_effect=sp.SmartPasteError(
                    f"smart-paste runtime root must not be a symlink: {secret_path}"
                ),
            ),
            mock.patch.object(
                sp,
                "notify",
                side_effect=lambda message, **_kwargs: notices.append(message),
            ),
            contextlib.redirect_stdout(output),
        ):
            code = sp.main(
                [
                    "--pane",
                    self.pane,
                    "--client",
                    self.client,
                    "--runtime-root",
                    str(unsafe_runtime),
                    "--json",
                ]
            )
        self.assertEqual(code, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["error"]["code"], "paste_rejected")
        self.assertIsNone(payload["receipt_path"])
        self.assertNotIn(secret_path, output.getvalue())
        self.assertNotIn(secret_path, notices[0])

    def test_non_json_cli_failure_does_not_echo_private_error(self) -> None:
        stderr = io.StringIO()
        notices: list[str] = []
        secret_path = "/Users/operator/private-image.png"
        with (
            mock.patch.object(
                sp,
                "smart_paste",
                side_effect=TransferError(f"authentication failed for {secret_path}"),
            ),
            mock.patch.object(
                sp,
                "notify",
                side_effect=lambda message, **_kwargs: notices.append(message),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            code = sp.main(
                [
                    "--pane",
                    self.pane,
                    "--client",
                    self.client,
                    "--runtime-root",
                    str(self.runtime),
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("auth_failed", stderr.getvalue())
        self.assertNotIn(secret_path, stderr.getvalue())
        self.assertNotIn(secret_path, notices[0])

    def test_cli_cancel_supersedes_inflight_gesture_without_injection(self) -> None:
        notices: list[str] = []
        with mock.patch.object(
            sp,
            "notify",
            side_effect=lambda message, **_kwargs: notices.append(message),
        ):
            code = sp.main(
                [
                    "--pane",
                    self.pane,
                    "--client",
                    self.client,
                    "--runtime-root",
                    str(self.runtime),
                    "--cancel",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(notices, ["Paste cancelled"])
        self.assertEqual(len(list(self.runtime.glob("pane-*.generation"))), 1)
        self.assertFalse(self.tmux.injections)


if __name__ == "__main__":
    unittest.main()
