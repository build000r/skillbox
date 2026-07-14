from __future__ import annotations

import base64
import copy
import json
import os
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.lib import clipboard_snapshot as cs


ROOT_DIR = Path(__file__).resolve().parent.parent
CLI = ROOT_DIR / "scripts" / "clipboard-snapshot"
PNG = (
    b"\x89PNG\r\n\x1a\n"
    + b"\x00\x00\x00\x0dIHDR"
    + (11).to_bytes(4, "big")
    + (7).to_bytes(4, "big")
    + b"\x08\x06\x00\x00\x00"
)
JPEG = (
    b"\xff\xd8\xff\xc0\x00\x0b\x08"
    + (5).to_bytes(2, "big")
    + (9).to_bytes(2, "big")
    + b"\x01\x01\x11\x00"
)
TIFF = (
    b"II*\x00\x08\x00\x00\x00"
    + b"\x02\x00"
    + b"\x00\x01\x04\x00\x01\x00\x00\x00"
    + (9).to_bytes(4, "little")
    + b"\x01\x01\x04\x00\x01\x00\x00\x00"
    + (5).to_bytes(4, "little")
    + b"\x00\x00\x00\x00"
)
HEIC = b"\x00\x00\x00\x18ftypheic" + b"fixture-heic"
PDF = b"%PDF-1.7\nfixture-pdf"


def payload(uti: str, **item: object) -> dict[str, object]:
    return {
        "change_count_before": 42,
        "change_count_after": 42,
        "types": [uti],
        "items": [{"uti": uti, **item}],
    }


class ClipboardSnapshotTests(unittest.TestCase):
    def test_classifies_and_materializes_png_without_mutating_payload(self) -> None:
        fixture = payload(
            "public.png",
            data_base64=base64.b64encode(PNG).decode(),
            width=11,
            height=7,
        )
        original = copy.deepcopy(fixture)
        with tempfile.TemporaryDirectory() as tmpdir:
            snap = cs.snapshot_from_payload(fixture, output_dir=Path(tmpdir))
            self.assertTrue(snap.ok)
            self.assertEqual(snap.kind, "image")
            self.assertEqual(snap.mime, "image/png")
            self.assertEqual((snap.width, snap.height), (11, 7))
            artifact = Path(snap.artifact or "")
            self.assertEqual(artifact.read_bytes(), PNG)
            self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(tmpdir).stat().st_mode & 0o777, 0o700)
        self.assertEqual(fixture, original)

    def test_classifies_jpeg_and_tiff(self) -> None:
        for uti, data, mime in (
            ("public.jpeg", JPEG, "image/jpeg"),
            ("public.tiff", TIFF, "image/tiff"),
        ):
            with self.subTest(uti=uti):
                snap = cs.snapshot_from_payload(
                    payload(
                        uti,
                        data_base64=base64.b64encode(data).decode(),
                        width=9,
                        height=5,
                    )
                )
                self.assertEqual(snap.kind, "image")
                self.assertEqual(snap.mime, mime)

    def test_classifies_heic_and_pdf_as_safe_document_paths(self) -> None:
        for uti, data, mime in (
            ("public.heic", HEIC, "image/heic"),
            ("com.adobe.pdf", PDF, "application/pdf"),
        ):
            with self.subTest(uti=uti), tempfile.TemporaryDirectory() as raw:
                snap = cs.snapshot_from_payload(
                    payload(uti, data_base64=base64.b64encode(data).decode()),
                    output_dir=Path(raw),
                )
                self.assertEqual(snap.kind, "document")
                self.assertEqual(snap.mime, mime)
                self.assertEqual(Path(snap.artifact or "").read_bytes(), data)

    def test_classifies_text_without_exposing_content(self) -> None:
        snap = cs.snapshot_from_payload(
            payload("public.utf8-plain-text", text="secret text")
        )
        public = snap.public_dict()
        self.assertEqual(snap.kind, "text")
        self.assertEqual(snap.byte_size, len("secret text"))
        self.assertNotIn("secret text", json.dumps(public))

    def test_classifies_finder_files_with_redacted_paths(self) -> None:
        snap = cs.snapshot_from_payload(
            payload(
                "public.file-url", paths=["/Users/b/Secret Plan.png", "/tmp/other.pdf"]
            )
        )
        public = snap.public_dict()
        self.assertEqual(snap.kind, "files")
        self.assertEqual(snap.file_count, 2)
        self.assertEqual(snap.file_names, ("Secret Plan.png", "other.pdf"))
        self.assertNotIn("/Users/b", json.dumps(public))
        self.assertNotIn("Secret Plan.png", json.dumps(public))
        self.assertEqual(public["file_names"], [])

    def test_empty_and_unsupported_are_distinct(self) -> None:
        empty = cs.snapshot_from_payload({"change_count": 1, "types": [], "items": []})
        unsupported = cs.snapshot_from_payload(
            payload("com.example.unknown", value="opaque")
        )
        self.assertEqual((empty.ok, empty.kind), (True, "empty"))
        self.assertEqual((unsupported.ok, unsupported.kind), (False, "unsupported"))
        self.assertEqual(unsupported.error["code"], "unsupported_type")  # type: ignore[index]

    def test_size_limit_corruption_and_race_fail_before_artifact(self) -> None:
        fixtures = [
            (
                payload("public.png", data_base64=base64.b64encode(PNG).decode()),
                4,
                "too_large",
            ),
            (
                payload(
                    "public.png", data_base64=base64.b64encode(b"not-png").decode()
                ),
                100,
                "corrupt_media",
            ),
            (
                {
                    **payload("public.png", data_base64=base64.b64encode(PNG).decode()),
                    "change_count_after": 43,
                },
                100,
                "clipboard_changed",
            ),
        ]
        for fixture, limit, code in fixtures:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmpdir:
                with self.assertRaises(cs.SnapshotError) as caught:
                    cs.snapshot_from_payload(
                        fixture, output_dir=Path(tmpdir), max_bytes=limit
                    )
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(list(Path(tmpdir).iterdir()), [])

    def test_missing_or_excessive_decoded_dimensions_are_rejected(self) -> None:
        fixtures = (
            payload("public.png", data_base64=base64.b64encode(PNG).decode()),
            payload(
                "public.png",
                data_base64=base64.b64encode(PNG).decode(),
                width=cs.DEFAULT_MAX_DIMENSION + 1,
                height=1,
            ),
            payload(
                "public.png",
                data_base64=base64.b64encode(PNG).decode(),
                width=20_000,
                height=20_000,
            ),
        )
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                with self.assertRaises(cs.SnapshotError) as caught:
                    cs.snapshot_from_payload(fixture)
                self.assertEqual(caught.exception.code, "corrupt_media")

    def test_encoded_header_dimension_bomb_is_rejected_even_when_decoded_size_is_small(
        self,
    ) -> None:
        bomb = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\x0dIHDR"
            + (cs.DEFAULT_MAX_DIMENSION + 1).to_bytes(4, "big")
            + (1).to_bytes(4, "big")
            + b"\x08\x06\x00\x00\x00"
        )
        with self.assertRaises(cs.SnapshotError) as caught:
            cs.snapshot_from_payload(
                payload(
                    "public.png",
                    data_base64=base64.b64encode(bomb).decode(),
                    width=1,
                    height=1,
                )
            )
        self.assertEqual(caught.exception.code, "corrupt_media")

    def test_cli_emits_stable_json_and_private_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fixture = Path(tmpdir) / "fixture.json"
            materialize = Path(tmpdir) / "out"
            fixture.write_text(
                json.dumps(
                    payload(
                        "public.png",
                        data_base64=base64.b64encode(PNG).decode(),
                        width=11,
                        height=7,
                    )
                ),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [
                    str(CLI),
                    "--fixture",
                    str(fixture),
                    "--materialize-dir",
                    str(materialize),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["kind"], "image")
            self.assertTrue(Path(result["artifact"]).is_file())

    @unittest.skipUnless(
        platform.system() == "Darwin"
        and os.environ.get("SKILLBOX_LIVE_CLIPBOARD_TEST") == "1",
        "live NSPasteboard proof requires explicit SKILLBOX_LIVE_CLIPBOARD_TEST=1",
    )
    def test_live_capture_does_not_change_pasteboard_generation(self) -> None:
        live = cs.capture_macos_payload()
        self.assertEqual(live["change_count_before"], live["change_count_after"])
        cs.snapshot_from_payload(live)


class CaptureBoundaryTests(unittest.TestCase):
    def test_runner_timeout_is_redacted(self) -> None:
        def timeout(
            *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired("osascript secret", 5)

        with mock.patch.object(platform, "system", return_value="Darwin"):
            with self.assertRaises(cs.SnapshotError) as caught:
                cs.capture_macos_payload(timeout)
        self.assertEqual(caught.exception.code, "capture_failed")
        self.assertNotIn("secret", str(caught.exception))

    def test_linux_wayland_capture_is_typed_and_generation_stable(self) -> None:
        png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\x0dIHDR"
            + (17).to_bytes(4, "big")
            + (9).to_bytes(4, "big")
            + b"\x08\x06\x00\x00\x00"
        )

        def runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            if command[1:] == ["--list-types"]:
                return subprocess.CompletedProcess(command, 0, b"image/png\ntext/plain\n", b"")
            if command[-1] == "image/png":
                return subprocess.CompletedProcess(command, 0, png, b"")
            return subprocess.CompletedProcess(command, 1, b"", b"unexpected")

        with mock.patch.object(platform, "system", return_value="Linux"):
            captured = cs.capture_linux_payload(runner)
        snapshot = cs.snapshot_from_payload(captured)
        self.assertEqual((snapshot.kind, snapshot.mime), ("image", "image/png"))
        self.assertEqual((snapshot.width, snapshot.height), (17, 9))
        self.assertEqual(
            captured["change_count_before"], captured["change_count_after"]
        )

    def test_linux_xclip_fallback_preserves_multiline_text(self) -> None:
        text = "one\ntwo\t$()"

        def runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            if command[0] == "wl-paste":
                raise FileNotFoundError("wl-paste")
            if "TARGETS" in command:
                return subprocess.CompletedProcess(command, 0, b"text/plain;charset=utf-8\n", b"")
            return subprocess.CompletedProcess(command, 0, text.encode(), b"")

        with mock.patch.object(platform, "system", return_value="Linux"):
            captured = cs.capture_linux_payload(runner)
        snapshot = cs.snapshot_from_payload(captured)
        self.assertEqual(snapshot.kind, "text")
        self.assertNotIn(text, json.dumps(snapshot.public_dict()))

    def test_windows_sta_capture_uses_same_typed_contract(self) -> None:
        png = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\x0dIHDR"
            + (12).to_bytes(4, "big")
            + (8).to_bytes(4, "big")
            + b"\x08\x06\x00\x00\x00"
        )
        payload = {
            "types": ["image/png"],
            "items": [
                {
                    "uti": "public.png",
                    "data_base64": base64.b64encode(png).decode(),
                    "width": 12,
                    "height": 8,
                }
            ],
        }

        def runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            self.assertIn("-STA", command)
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

        with mock.patch.object(platform, "system", return_value="Windows"):
            captured = cs.capture_windows_payload(runner)
        snapshot = cs.snapshot_from_payload(captured)
        self.assertEqual((snapshot.kind, snapshot.width, snapshot.height), ("image", 12, 8))

    def test_operator_dispatch_rejects_unknown_platform_without_capture(self) -> None:
        with mock.patch.object(platform, "system", return_value="Plan9"):
            with self.assertRaises(cs.SnapshotError) as caught:
                cs.capture_operator_payload()
        self.assertEqual(caught.exception.code, "unsupported_platform")


if __name__ == "__main__":
    unittest.main()
