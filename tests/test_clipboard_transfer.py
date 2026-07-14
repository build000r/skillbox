from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from scripts.lib import clipboard_transfer as ct


class ClipboardTransferReceiverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "store"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def receive(self, data: bytes, **overrides: object) -> dict[str, object]:
        args: dict[str, object] = {
            "expected_sha256": self.digest(data),
            "expected_size": len(data),
            "extension": "png",
            "root": self.root,
        }
        args.update(overrides)
        return ct.receive_artifact(io.BytesIO(data), **args)

    def test_atomic_success_has_private_modes_and_exact_receipt(self) -> None:
        data = b"\x89PNG\r\n\x1a\nfixture"
        receipt = self.receive(data)
        path = Path(str(receipt["path"]))
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertFalse(receipt["reused"])
        self.assertEqual(receipt["sha256"], self.digest(data))
        self.assertFalse(list(self.root.glob(".incoming-*")))

    def test_partial_write_is_removed(self) -> None:
        data = b"partial"
        with self.assertRaisesRegex(ct.TransferError, "partial artifact"):
            ct.receive_artifact(
                io.BytesIO(data),
                expected_sha256=self.digest(data + b"missing"),
                expected_size=len(data) + 7,
                extension="png",
                root=self.root,
            )
        self.assertFalse(list(self.root.glob(".incoming-*")))
        self.assertFalse(list(self.root.glob("*.png")))

    def test_hash_mismatch_is_rejected_and_cleaned(self) -> None:
        with self.assertRaisesRegex(ct.TransferError, "sha256 mismatch"):
            self.receive(b"actual", expected_sha256=self.digest(b"different"))
        self.assertFalse(list(self.root.glob(".incoming-*")))
        self.assertFalse(list(self.root.glob("*.png")))

    def test_declared_size_cannot_hide_trailing_bytes(self) -> None:
        data = b"1234"
        with self.assertRaisesRegex(ct.TransferError, "beyond declared size"):
            ct.receive_artifact(
                io.BytesIO(data),
                expected_sha256=self.digest(data[:3]),
                expected_size=3,
                extension="png",
                root=self.root,
            )

    def test_duplicate_reuses_verified_artifact(self) -> None:
        data = b"same"
        first = self.receive(data)
        second = self.receive(data)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(
            [path.resolve() for path in self.root.glob("*.png")],
            [Path(str(first["path"]))],
        )

    def test_existing_corrupt_content_address_fails_closed(self) -> None:
        data = b"expected"
        self.root.mkdir(mode=0o700)
        path = self.root / f"{self.digest(data)}.png"
        path.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ct.TransferError, "does not match receipt"):
            self.receive(data)

    def test_symlink_store_and_artifact_are_refused(self) -> None:
        real = Path(self.tmp.name) / "real"
        real.mkdir()
        self.root.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ct.TransferError, "not a real directory"):
            self.receive(b"x")

        self.root.unlink()
        self.root.mkdir(mode=0o700)
        data = b"y"
        outside = Path(self.tmp.name) / "outside"
        outside.write_bytes(data)
        (self.root / f"{self.digest(data)}.png").symlink_to(outside)
        with self.assertRaisesRegex(ct.TransferError, "not a regular file"):
            self.receive(data)

    def test_traversal_and_unsafe_target_are_refused(self) -> None:
        for extension in ("../png", "/tmp/x", "png;id", ""):
            with self.subTest(extension=extension):
                with self.assertRaises(ct.TransferError):
                    ct.validate_extension(extension)
        for target in ("-oProxyCommand=id", "host;id", "user@host/path", ""):
            with self.subTest(target=target):
                with self.assertRaises(ct.TransferError):
                    ct.validate_target(target)

    def test_size_limit_rejects_before_read(self) -> None:
        with self.assertRaisesRegex(ct.TransferError, "size must be"):
            ct.receive_artifact(
                io.BytesIO(b"x"),
                expected_sha256=self.digest(b"x"),
                expected_size=2,
                extension="png",
                root=self.root,
                max_bytes=1,
            )

    def test_ttl_and_quota_cleanup_only_touch_owned_artifacts(self) -> None:
        first = self.receive(b"a" * 10)
        second = self.receive(b"b" * 10)
        first_path = Path(str(first["path"]))
        second_path = Path(str(second["path"]))
        old = time.time() - 100
        os.utime(first_path, (old, old))
        report = ct.cleanup_store(
            self.root, ttl_seconds=50, quota_bytes=100, now=time.time()
        )
        self.assertEqual(report["removed_files"], 1)
        self.assertFalse(first_path.exists())
        self.assertTrue(second_path.exists())

        third = self.receive(b"c" * 10)
        third_path = Path(str(third["path"]))
        os.utime(second_path, (old + 1, old + 1))
        os.utime(third_path, (old + 2, old + 2))
        report = ct.cleanup_store(
            self.root, ttl_seconds=10_000, quota_bytes=10, now=time.time()
        )
        self.assertEqual(report["remaining_bytes"], 10)
        self.assertFalse(second_path.exists())
        self.assertTrue(third_path.exists())

    def test_cleanup_refuses_symlink_even_when_name_looks_owned(self) -> None:
        self.root.mkdir(mode=0o700)
        outside = Path(self.tmp.name) / "outside"
        outside.write_bytes(b"secret")
        (self.root / f"{'a' * 64}.png").symlink_to(outside)
        with self.assertRaisesRegex(ct.TransferError, "symlink found"):
            ct.cleanup_store(self.root)
        self.assertEqual(outside.read_bytes(), b"secret")

    def test_concurrent_same_digest_is_isolated_and_deduplicated(self) -> None:
        data = b"concurrent" * 10_000
        receipts: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def run() -> None:
            try:
                receipts.append(self.receive(data))
            except BaseException as exc:  # test captures thread failures
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertEqual(len(receipts), 4)
        self.assertEqual(sum(not bool(item["reused"]) for item in receipts), 1)
        self.assertEqual(len(list(self.root.glob("*.png"))), 1)
        self.assertFalse(list(self.root.glob(".incoming-*")))

    def test_delete_removes_only_exact_owned_content_address(self) -> None:
        data = b"canceled"
        receipt = self.receive(data)
        result = ct.delete_artifact(
            sha256=self.digest(data), extension="png", root=self.root
        )
        self.assertTrue(result["removed"])
        self.assertFalse(Path(str(receipt["path"])).exists())
        repeated = ct.delete_artifact(
            sha256=self.digest(data), extension="png", root=self.root
        )
        self.assertFalse(repeated["removed"])

    def test_delete_refuses_symlink_and_digest_mismatch(self) -> None:
        self.root.mkdir(mode=0o700)
        digest = self.digest(b"expected")
        outside = Path(self.tmp.name) / "outside"
        outside.write_bytes(b"expected")
        (self.root / f"{digest}.png").symlink_to(outside)
        with self.assertRaisesRegex(ct.TransferError, "regular file"):
            ct.delete_artifact(sha256=digest, extension="png", root=self.root)
        self.assertTrue(outside.exists())


class ClipboardTransferClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "fixture.png"
        self.path.write_bytes(b"image-fixture")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_client_streams_exact_bytes_and_verifies_receipt(self) -> None:
        observed: dict[str, object] = {}

        def runner(
            command: object, **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            data = kwargs["input"]
            assert isinstance(data, bytes)
            digest = hashlib.sha256(data).hexdigest()
            observed["command"] = command
            observed["data"] = data
            receipt = {
                "schema_version": 1,
                "ok": True,
                "path": f"/home/u/.cache/skillbox/paste-artifacts/{digest}.png",
                "sha256": digest,
                "byte_size": len(data),
                "mode": "0600",
                "reused": False,
                "cleanup": {
                    "removed_files": 0,
                    "removed_bytes": 0,
                    "remaining_bytes": len(data),
                },
            }
            return subprocess.CompletedProcess(
                command, 0, json.dumps(receipt).encode(), b""
            )

        receipt = ct.transfer_artifact(
            self.path, ssh_target="skillbox@devbox", runner=runner
        )
        self.assertEqual(observed["data"], self.path.read_bytes())
        self.assertEqual(observed["command"][0:2], ("ssh", "-o"))
        self.assertEqual(receipt["mode"], "0600")

    def test_path_replacement_after_capture_cannot_change_sent_bytes(self) -> None:
        original = self.path.read_bytes()
        replacement = b"different-private-file"

        def runner(
            command: object, **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            self.path.unlink()
            self.path.write_bytes(replacement)
            sent = kwargs["input"]
            self.assertEqual(sent, original)
            digest = hashlib.sha256(original).hexdigest()
            receipt = {
                "schema_version": 1,
                "ok": True,
                "path": f"/home/u/.cache/skillbox/paste-artifacts/{digest}.png",
                "sha256": digest,
                "byte_size": len(original),
                "mode": "0600",
                "reused": False,
                "cleanup": {
                    "removed_files": 0,
                    "removed_bytes": 0,
                    "remaining_bytes": len(original),
                },
            }
            return subprocess.CompletedProcess(
                command, 0, json.dumps(receipt).encode(), b""
            )

        ct.transfer_artifact(self.path, ssh_target="skillbox@devbox", runner=runner)
        self.assertEqual(self.path.read_bytes(), replacement)

    def test_timeout_is_reported_without_retrying(self) -> None:
        def runner(
            *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            raise subprocess.TimeoutExpired("ssh", 0.01)

        with self.assertRaisesRegex(ct.TransferError, "timed out"):
            ct.transfer_artifact(
                self.path,
                ssh_target="devbox",
                timeout_seconds=0.01,
                runner=runner,
            )

    def test_remote_auth_failure_is_redacted_and_fails_closed(self) -> None:
        def failed(
            command: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(command, 23, b"", b"Permission denied")

        with self.assertRaisesRegex(ct.TransferError, "authentication failed"):
            ct.transfer_artifact(self.path, ssh_target="devbox", runner=failed)

    def test_remote_stderr_is_classified_without_secret_leakage(self) -> None:
        secret = "/home/user/private/secret-image.png"

        def failed(
            command: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(
                command, 23, b"", f"decoder failed at {secret}".encode()
            )

        with self.assertRaises(ct.TransferError) as caught:
            ct.transfer_artifact(self.path, ssh_target="devbox", runner=failed)
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("receiver rejected", str(caught.exception))

    def test_receipt_schema_mismatch_fails_closed(self) -> None:
        def malformed(
            command: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(command, 0, b'{"ok":true}', b"")

        with self.assertRaisesRegex(ct.TransferError, "unknown receipt schema"):
            ct.transfer_artifact(self.path, ssh_target="devbox", runner=malformed)

    def test_receipt_path_and_cleanup_metadata_cannot_escape_contract(self) -> None:
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()

        def response(path: str, cleanup: object) -> object:
            def runner(
                command: object, **_kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                payload = {
                    "schema_version": 1,
                    "ok": True,
                    "path": path,
                    "sha256": digest,
                    "byte_size": self.path.stat().st_size,
                    "mode": "0600",
                    "reused": False,
                    "cleanup": cleanup,
                }
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(payload).encode(), b""
                )

            return runner

        good_cleanup = {
            "removed_files": 0,
            "removed_bytes": 0,
            "remaining_bytes": self.path.stat().st_size,
        }
        for path in (
            f"/tmp/{digest}.png",
            f"/home/u/.cache/skillbox/paste-artifacts/../{digest}.png",
            "/home/u/.cache/skillbox/paste-artifacts/wrong.png",
        ):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ct.TransferError, "path escaped"):
                    ct.transfer_artifact(
                        self.path,
                        ssh_target="devbox",
                        runner=response(path, good_cleanup),  # type: ignore[arg-type]
                    )
        with self.assertRaisesRegex(ct.TransferError, "cleanup metadata"):
            ct.transfer_artifact(
                self.path,
                ssh_target="devbox",
                runner=response(  # type: ignore[arg-type]
                    f"/home/u/.cache/skillbox/paste-artifacts/{digest}.png",
                    {"removed_files": -1},
                ),
            )

    def test_receipt_rejects_bool_as_integer_and_non_bool_reuse(self) -> None:
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        base = {
            "schema_version": 1,
            "ok": True,
            "path": f"/home/u/.cache/skillbox/paste-artifacts/{digest}.png",
            "sha256": digest,
            "byte_size": self.path.stat().st_size,
            "mode": "0600",
            "reused": False,
            "cleanup": {
                "removed_files": 0,
                "removed_bytes": 0,
                "remaining_bytes": self.path.stat().st_size,
            },
        }

        for field, value in (
            ("schema_version", True),
            ("byte_size", True),
            ("reused", 0),
            ("cleanup.removed_files", False),
        ):
            with self.subTest(field=field):
                payload = json.loads(json.dumps(base))
                if field.startswith("cleanup."):
                    payload["cleanup"][field.split(".", 1)[1]] = value
                else:
                    payload[field] = value

                def runner(
                    command: object, **_kwargs: object
                ) -> subprocess.CompletedProcess[bytes]:
                    return subprocess.CompletedProcess(
                        command, 0, json.dumps(payload).encode(), b""
                    )

                with self.assertRaises(ct.TransferError):
                    ct.transfer_artifact(
                        self.path, ssh_target="devbox", runner=runner
                    )

    def test_receiver_cli_cleans_partial_stream_after_cancellation(self) -> None:
        digest = hashlib.sha256(b"complete").hexdigest()
        code = ct.main(
            [
                "put",
                "--sha256",
                digest,
                "--size",
                "8",
                "--extension",
                "png",
                "--root",
                str(Path(self.tmp.name) / "remote"),
            ],
            stdin=io.BytesIO(b"cut"),
        )
        self.assertEqual(code, 1)
        self.assertFalse(list((Path(self.tmp.name) / "remote").glob(".incoming-*")))

    def test_remote_cleanup_uses_exact_argv_and_validates_receipt(self) -> None:
        digest = hashlib.sha256(b"fixture").hexdigest()
        observed: list[str] = []

        def runner(
            command: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            observed.extend(command)  # type: ignore[arg-type]
            payload = {
                "schema_version": 1,
                "ok": True,
                "sha256": digest,
                "removed": True,
            }
            return subprocess.CompletedProcess(
                command, 0, json.dumps(payload).encode(), b""
            )

        receipt = ct.delete_remote_artifact(
            ssh_target="skillbox@devbox",
            sha256=digest,
            extension=".png",
            runner=runner,
        )
        self.assertTrue(receipt["removed"])
        self.assertEqual(observed[-5:], ["delete", "--sha256", digest, "--extension", "png"])


if __name__ == "__main__":
    unittest.main()
