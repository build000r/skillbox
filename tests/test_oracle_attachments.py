from __future__ import annotations

import hashlib
import os
import stat
import struct
import tempfile
import traceback
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from runtime_manager import oracle_attachments as attachments


class OracleAttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.base = Path(os.path.realpath(self._temporary.name))
        self.allowed = self.base / "allowed"
        self.allowed.mkdir(mode=0o700)
        self.stage_parent = self.base / "stage"
        self.stage_parent.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def policy(self, **overrides: object) -> attachments.AttachmentPolicy:
        values: dict[str, object] = {
            "allowed_roots": (self.allowed,),
            "temp_parent": self.stage_parent,
            "max_attachments": 4,
            "max_source_bytes": 32_000,
            "max_total_source_bytes": 64_000,
            "max_expanded_files": 8,
            "max_archive_entries": 12,
            "max_archive_member_bytes": 16_000,
            "max_total_expanded_bytes": 48_000,
            "max_compression_ratio": 100,
        }
        values.update(overrides)
        return attachments.AttachmentPolicy(**values)

    def write(self, name: str, data: bytes) -> Path:
        path = self.allowed / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def make_zip(
        self,
        name: str,
        members: list[tuple[str | zipfile.ZipInfo, bytes]],
        *,
        compression: int = zipfile.ZIP_DEFLATED,
    ) -> Path:
        path = self.allowed / name
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            for member_name, data in members:
                archive.writestr(member_name, data)
        return path

    def make_zip64_with_entry_count(
        self,
        name: str,
        source: Path,
        entry_count: int,
    ) -> Path:
        encoded = source.read_bytes()
        eocd_offset = encoded.rfind(b"PK\x05\x06")
        self.assertGreaterEqual(eocd_offset, 0)
        (
            signature,
            _disk_number,
            _central_disk,
            _entries_on_disk,
            _entry_count,
            central_bytes,
            central_offset,
            comment_bytes,
        ) = struct.unpack_from("<4s4H2LH", encoded, eocd_offset)
        self.assertEqual(signature, b"PK\x05\x06")
        self.assertEqual(comment_bytes, 0)

        prefix = encoded[:eocd_offset]
        zip64_offset = len(prefix)
        zip64_eocd = struct.pack(
            "<4sQ2H2L4Q",
            b"PK\x06\x06",
            44,
            45,
            45,
            0,
            0,
            entry_count,
            entry_count,
            central_bytes,
            central_offset,
        )
        locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, zip64_offset, 1)
        sentinel_eocd = struct.pack(
            "<4s4H2LH",
            b"PK\x05\x06",
            0,
            0,
            0xFFFF,
            0xFFFF,
            0xFFFFFFFF,
            0xFFFFFFFF,
            0,
        )
        path = self.allowed / name
        path.write_bytes(prefix + zip64_eocd + locator + sentinel_eocd)
        return path

    def assert_rejected(self, code: str, action: object) -> None:
        with self.assertRaises(attachments.AttachmentValidationError) as raised:
            action()  # type: ignore[operator]
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), "oracle attachment validation rejected")

    def test_stages_plain_files_and_bounded_archive_members_privately(self) -> None:
        note = self.write("notes.txt", b"grounded notes\n")
        archive = self.make_zip(
            "evidence.zip",
            [
                ("folder/report.md", b"# Report\n"),
                ("data.json", b'{"ok":true}\n'),
            ],
        )
        specs = [
            attachments.AttachmentSpec(note, "text/plain"),
            attachments.AttachmentSpec(archive, "application/zip"),
        ]

        batch_root: Path
        staged_paths: list[Path]
        with attachments.prepare_attachments(specs, policy=self.policy()) as batch:
            batch_root = batch.root
            staged_paths = [item.staged_path for item in batch.attachments]
            self.assertEqual(batch.source_count, 2)
            self.assertEqual(
                batch.source_bytes, note.stat().st_size + archive.stat().st_size
            )
            self.assertEqual(batch.expanded_bytes, 15 + 9 + 12)
            self.assertEqual(
                [item.display_name for item in batch.attachments],
                ["attachment-001.txt", "attachment-002.md", "attachment-003.json"],
            )
            self.assertEqual(
                [item.mime_type for item in batch.attachments],
                ["text/plain", "text/markdown", "application/json"],
            )
            self.assertEqual(stat.S_IMODE(batch.root.stat().st_mode), 0o700)
            self.assertNotIn("notes", str(batch.attachments))
            self.assertNotIn("evidence", str(batch.attachments))
            for item in batch.attachments:
                metadata = item.staged_path.stat()
                self.assertTrue(item.staged_path.is_relative_to(batch.root))
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
                self.assertEqual(metadata.st_nlink, 1)
                self.assertEqual(item.bytes, metadata.st_size)
                self.assertEqual(
                    item.sha256,
                    hashlib.sha256(item.staged_path.read_bytes()).hexdigest(),
                )

        self.assertFalse(batch_root.exists())
        self.assertTrue(all(not path.exists() for path in staged_paths))
        self.assertTrue(batch.closed)
        batch.close()

    def test_rejects_attachment_count_file_size_and_total_source_bytes(self) -> None:
        first = self.write("first.txt", b"12345")
        second = self.write("second.txt", b"67890")
        specs = [
            attachments.AttachmentSpec(first, "text/plain"),
            attachments.AttachmentSpec(second, "text/plain"),
        ]
        self.assert_rejected(
            "attachment_count_exceeded",
            lambda: attachments.prepare_attachments(
                specs, policy=self.policy(max_attachments=1)
            ),
        )
        self.assert_rejected(
            "source_bytes_exceeded",
            lambda: attachments.prepare_attachments(
                specs[:1],
                policy=self.policy(
                    max_source_bytes=4,
                    max_total_source_bytes=10,
                ),
            ),
        )
        self.assert_rejected(
            "total_source_bytes_exceeded",
            lambda: attachments.prepare_attachments(
                specs,
                policy=self.policy(
                    max_source_bytes=6,
                    max_total_source_bytes=9,
                ),
            ),
        )

    def test_bounds_arbitrary_iterables_and_sanitizes_iteration_errors(self) -> None:
        source = self.write("iterable.txt", b"bounded")
        spec = attachments.AttachmentSpec(source, "text/plain")
        secret = "ITERATOR_SECRET_MUST_NOT_ESCAPE"

        class SecretIterationFailure(BaseException):
            pass

        class InfiniteAttachments:
            def __init__(self) -> None:
                self.calls = 0
                self.close_calls = 0

            def __iter__(self) -> InfiniteAttachments:
                return self

            def __next__(self) -> attachments.AttachmentSpec:
                self.calls += 1
                return spec

            def close(self) -> None:
                self.close_calls += 1
                raise SecretIterationFailure(secret)

        infinite = InfiniteAttachments()
        before = set(self.stage_parent.iterdir())
        with self.assertRaises(attachments.AttachmentValidationError) as overflow:
            attachments.prepare_attachments(
                infinite,
                policy=self.policy(max_attachments=4),
            )
        self.assertEqual(overflow.exception.code, "attachment_count_exceeded")
        self.assertEqual(
            str(overflow.exception),
            "oracle attachment validation rejected",
        )
        self.assertIsNone(overflow.exception.__context__)
        self.assertNotIn(
            secret,
            "".join(
                traceback.format_exception(
                    type(overflow.exception),
                    overflow.exception,
                    overflow.exception.__traceback__,
                )
            ),
        )
        self.assertEqual(infinite.calls, 5)
        self.assertEqual(infinite.close_calls, 1)
        self.assertEqual(set(self.stage_parent.iterdir()), before)

        class IterFails:
            def __iter__(self) -> IterFails:
                raise SecretIterationFailure(secret)

        class NextFails:
            def __iter__(self) -> NextFails:
                return self

            def __next__(self) -> attachments.AttachmentSpec:
                raise SecretIterationFailure(secret)

        for value in (IterFails(), NextFails()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(attachments.AttachmentValidationError) as raised:
                    attachments.prepare_attachments(value, policy=self.policy())
                error = raised.exception
                self.assertEqual(error.code, "attachment_list_invalid")
                self.assertEqual(
                    str(error),
                    "oracle attachment validation rejected",
                )
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)
                rendered = "".join(
                    traceback.format_exception(
                        type(error),
                        error,
                        error.__traceback__,
                    )
                )
                self.assertNotIn(secret, rendered)
        self.assertEqual(set(self.stage_parent.iterdir()), before)

    def test_rejects_mime_declaration_suffix_content_and_policy_drift(self) -> None:
        text = self.write("note.txt", b"hello\n")
        fake_png = self.write("image.png", b"not a png")
        disguised_pdf = self.write("disguised.txt", b"%PDF-1.7\n")
        unsupported = self.write("payload.bin", b"binary")

        self.assert_rejected(
            "mime_mismatch",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(text, "application/pdf")],
                policy=self.policy(),
            ),
        )
        self.assert_rejected(
            "mime_content_mismatch",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(fake_png, "image/png")],
                policy=self.policy(),
            ),
        )
        self.assert_rejected(
            "mime_content_mismatch",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(disguised_pdf, "text/plain")],
                policy=self.policy(),
            ),
        )
        self.assert_rejected(
            "mime_unsupported",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(unsupported, "text/plain")],
                policy=self.policy(),
            ),
        )
        self.assert_rejected(
            "mime_not_allowed",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(text, "text/plain")],
                policy=self.policy(allowed_mime_types=frozenset({"application/pdf"})),
            ),
        )

    def test_rejects_noncanonical_outside_and_symlink_paths(self) -> None:
        source = self.write("source.txt", b"safe")
        outside = self.base / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        symlink = self.allowed / "linked.txt"
        symlink.symlink_to(source)
        linked_parent = self.allowed / "linked-parent"
        real_parent = self.allowed / "real-parent"
        real_parent.mkdir()
        (real_parent / "nested.txt").write_text("nested", encoding="utf-8")
        linked_parent.symlink_to(real_parent, target_is_directory=True)

        self.assert_rejected(
            "path_not_canonical",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(Path("source.txt"), "text/plain")],
                policy=self.policy(),
            ),
        )
        self.assert_rejected(
            "path_not_canonical",
            lambda: attachments.prepare_attachments(
                [
                    attachments.AttachmentSpec(
                        self.allowed / "real-parent" / ".." / "source.txt",
                        "text/plain",
                    )
                ],
                policy=self.policy(),
            ),
        )
        self.assert_rejected(
            "path_not_allowed",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(outside, "text/plain")],
                policy=self.policy(),
            ),
        )
        self.assert_rejected(
            "path_not_canonical",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(symlink, "text/plain")],
                policy=self.policy(),
            ),
        )
        self.assert_rejected(
            "path_not_canonical",
            lambda: attachments.prepare_attachments(
                [
                    attachments.AttachmentSpec(
                        linked_parent / "nested.txt",
                        "text/plain",
                    )
                ],
                policy=self.policy(),
            ),
        )

    def test_rejects_hardlinks_fifos_and_duplicate_sources(self) -> None:
        source = self.write("source.txt", b"safe")
        hardlink = self.allowed / "hardlink.txt"
        os.link(source, hardlink)
        fifo = self.allowed / "pipe.txt"
        os.mkfifo(fifo, 0o600)

        self.assert_rejected(
            "source_hardlinked",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(source, "text/plain")],
                policy=self.policy(),
            ),
        )
        self.assert_rejected(
            "source_not_regular",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(fifo, "text/plain")],
                policy=self.policy(),
            ),
        )
        hardlink.unlink()
        self.assert_rejected(
            "duplicate_source",
            lambda: attachments.prepare_attachments(
                [
                    attachments.AttachmentSpec(source, "text/plain"),
                    attachments.AttachmentSpec(source, "text/plain"),
                ],
                policy=self.policy(),
            ),
        )

    def test_detects_content_mutation_during_descriptor_read(self) -> None:
        source = self.write("race.txt", b"A" * 128)
        original_read = attachments.os.read
        changed = False

        def racing_read(descriptor: int, amount: int) -> bytes:
            nonlocal changed
            chunk = original_read(descriptor, amount)
            if chunk and not changed:
                changed = True
                source.write_bytes(b"B" * 128)
            return chunk

        with mock.patch.object(attachments.os, "read", side_effect=racing_read):
            self.assert_rejected(
                "source_changed",
                lambda: attachments.prepare_attachments(
                    [attachments.AttachmentSpec(source, "text/plain")],
                    policy=self.policy(),
                ),
            )

    def test_detects_path_replacement_between_lstat_and_open(self) -> None:
        source = self.write("race.txt", b"original")
        replacement = self.write("replacement.txt", b"replacement")
        original_open = attachments.os.open
        replaced = False

        def racing_open(
            path: object,
            flags: int,
            mode: int = 0o777,
            **kwargs: object,
        ) -> int:
            nonlocal replaced
            if (
                kwargs.get("dir_fd") is None
                and Path(path) == source
                and flags & os.O_ACCMODE == os.O_RDONLY
                and not replaced
            ):
                replaced = True
                os.replace(replacement, source)
            return original_open(path, flags, mode, **kwargs)

        with mock.patch.object(attachments.os, "open", side_effect=racing_open):
            self.assert_rejected(
                "source_changed",
                lambda: attachments.prepare_attachments(
                    [attachments.AttachmentSpec(source, "text/plain")],
                    policy=self.policy(),
                ),
            )

    def test_rejects_zip_slip_noncanonical_and_duplicate_member_paths(self) -> None:
        bad_names = [
            "../escape.txt",
            "/absolute.txt",
            "folder\\windows.txt",
            "folder/../escape.txt",
            "folder//alias.txt",
        ]
        for index, member_name in enumerate(bad_names):
            with self.subTest(member_name=member_name):
                archive = self.make_zip(
                    f"bad-{index}.zip",
                    [(member_name, b"bad")],
                )
                self.assert_rejected(
                    "archive_path_invalid",
                    lambda archive=archive: attachments.prepare_attachments(
                        [attachments.AttachmentSpec(archive, "application/zip")],
                        policy=self.policy(),
                    ),
                )

        duplicate = self.allowed / "duplicate.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(duplicate, "w") as archive:
                archive.writestr("same.txt", b"one")
                archive.writestr("same.txt", b"two")
        self.assert_rejected(
            "archive_duplicate_path",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(duplicate, "application/zip")],
                policy=self.policy(),
            ),
        )

        case_alias = self.make_zip(
            "case-alias.zip",
            [("A.txt", b"one"), ("a.txt", b"two")],
        )
        self.assert_rejected(
            "archive_duplicate_path",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(case_alias, "application/zip")],
                policy=self.policy(),
            ),
        )

        prefix_collision = self.make_zip(
            "prefix.zip",
            [("node.txt", b"file"), ("node.txt/child.txt", b"child")],
        )
        self.assert_rejected(
            "archive_duplicate_path",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(prefix_collision, "application/zip")],
                policy=self.policy(),
            ),
        )

    def test_rejects_archive_links_special_files_and_nested_archives(self) -> None:
        link_info = zipfile.ZipInfo("link.txt")
        link_info.create_system = 3
        link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
        link_archive = self.make_zip("link.zip", [(link_info, b"target")])
        self.assert_rejected(
            "archive_unsafe_type",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(link_archive, "application/zip")],
                policy=self.policy(),
            ),
        )

        fifo_info = zipfile.ZipInfo("fifo.txt")
        fifo_info.create_system = 3
        fifo_info.external_attr = (stat.S_IFIFO | 0o600) << 16
        fifo_archive = self.make_zip("fifo.zip", [(fifo_info, b"fifo")])
        self.assert_rejected(
            "archive_unsafe_type",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(fifo_archive, "application/zip")],
                policy=self.policy(),
            ),
        )

        inner = self.make_zip("inner-source.zip", [("inner.txt", b"inner")])
        nested = self.make_zip(
            "nested.zip",
            [("inner.zip", inner.read_bytes())],
        )
        self.assert_rejected(
            "nested_archive_forbidden",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(nested, "application/zip")],
                policy=self.policy(),
            ),
        )

    def test_rejects_archive_count_size_ratio_and_compression_bombs(self) -> None:
        too_many = self.make_zip(
            "too-many.zip",
            [(f"{index}.txt", b"x") for index in range(4)],
        )
        self.assert_rejected(
            "archive_entries_exceeded",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(too_many, "application/zip")],
                policy=self.policy(max_archive_entries=3),
            ),
        )
        self.assert_rejected(
            "expanded_file_count_exceeded",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(too_many, "application/zip")],
                policy=self.policy(max_expanded_files=3, max_attachments=1),
            ),
        )

        oversized = self.make_zip("oversized.zip", [("large.txt", b"A" * 256)])
        self.assert_rejected(
            "archive_member_bytes_exceeded",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(oversized, "application/zip")],
                policy=self.policy(
                    max_archive_member_bytes=128,
                    max_total_expanded_bytes=256,
                ),
            ),
        )
        self.assert_rejected(
            "archive_compression_ratio_exceeded",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(oversized, "application/zip")],
                policy=self.policy(max_compression_ratio=2),
            ),
        )

        aggregate = self.make_zip(
            "aggregate.zip",
            [("one.txt", b"123456"), ("two.txt", b"abcdef")],
            compression=zipfile.ZIP_STORED,
        )
        self.assert_rejected(
            "expanded_bytes_exceeded",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(aggregate, "application/zip")],
                policy=self.policy(
                    max_archive_member_bytes=10,
                    max_total_expanded_bytes=11,
                ),
            ),
        )

        unsupported = self.make_zip(
            "bzip.zip",
            [("file.txt", b"content")],
            compression=zipfile.ZIP_BZIP2,
        )
        self.assert_rejected(
            "archive_compression_unsupported",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(unsupported, "application/zip")],
                policy=self.policy(),
            ),
        )

    def test_preflights_eocd_zip64_and_central_directory_before_zipfile(self) -> None:
        source = self.make_zip("preflight-source.zip", [("file.txt", b"content")])
        valid_zip64 = self.make_zip64_with_entry_count(
            "valid-zip64.zip",
            source,
            1,
        )
        with attachments.prepare_attachments(
            [attachments.AttachmentSpec(valid_zip64, "application/zip")],
            policy=self.policy(),
        ) as batch:
            self.assertEqual(len(batch.attachments), 1)
            self.assertEqual(batch.attachments[0].staged_path.read_bytes(), b"content")

        excessive_eocd = self.allowed / "excessive-eocd.zip"
        eocd_bytes = bytearray(source.read_bytes())
        eocd_offset = eocd_bytes.rfind(b"PK\x05\x06")
        self.assertGreaterEqual(eocd_offset, 0)
        struct.pack_into("<HH", eocd_bytes, eocd_offset + 8, 13, 13)
        excessive_eocd.write_bytes(eocd_bytes)

        excessive_zip64 = self.make_zip64_with_entry_count(
            "excessive-zip64.zip",
            source,
            13,
        )

        lying_eocd = self.make_zip(
            "lying-eocd-source.zip",
            [(f"{index}.txt", b"x") for index in range(13)],
        )
        lying_bytes = bytearray(lying_eocd.read_bytes())
        lying_offset = lying_bytes.rfind(b"PK\x05\x06")
        self.assertGreaterEqual(lying_offset, 0)
        struct.pack_into("<HH", lying_bytes, lying_offset + 8, 1, 1)
        lying_eocd.write_bytes(lying_bytes)

        cases = (
            (
                excessive_eocd,
                "archive_entries_exceeded",
                self.policy(max_archive_entries=12),
            ),
            (
                excessive_zip64,
                "archive_entries_exceeded",
                self.policy(max_archive_entries=12),
            ),
            (
                lying_eocd,
                "archive_entries_exceeded",
                self.policy(max_archive_entries=12),
            ),
            (
                source,
                "archive_central_directory_exceeded",
                self.policy(max_archive_central_directory_bytes=1),
            ),
        )
        for archive, code, policy in cases:
            with self.subTest(archive=archive.name):
                with mock.patch.object(
                    attachments.zipfile,
                    "ZipFile",
                    side_effect=AssertionError("ZipFile parsing ran before preflight"),
                ) as parser:
                    self.assert_rejected(
                        code,
                        lambda archive=archive, policy=policy: (
                            attachments.prepare_attachments(
                                [
                                    attachments.AttachmentSpec(
                                        archive,
                                        "application/zip",
                                    )
                                ],
                                policy=policy,
                            )
                        ),
                    )
                    parser.assert_not_called()

    def test_rejects_encrypted_zip_before_zipfile_parsing(self) -> None:
        encrypted = self.make_zip(
            "encrypted.zip",
            [("secret.txt", b"encrypted-content")],
            compression=zipfile.ZIP_STORED,
        )
        encoded = bytearray(encrypted.read_bytes())
        local_offset = encoded.find(b"PK\x03\x04")
        central_offset = encoded.find(b"PK\x01\x02")
        self.assertGreaterEqual(local_offset, 0)
        self.assertGreaterEqual(central_offset, 0)
        local_flags = struct.unpack_from("<H", encoded, local_offset + 6)[0]
        central_flags = struct.unpack_from("<H", encoded, central_offset + 8)[0]
        struct.pack_into("<H", encoded, local_offset + 6, local_flags | 0x1)
        struct.pack_into("<H", encoded, central_offset + 8, central_flags | 0x1)
        encrypted.write_bytes(encoded)

        with mock.patch.object(
            attachments.zipfile,
            "ZipFile",
            side_effect=AssertionError("encrypted ZIP reached ZipFile"),
        ) as parser:
            self.assert_rejected(
                "archive_encrypted",
                lambda: attachments.prepare_attachments(
                    [attachments.AttachmentSpec(encrypted, "application/zip")],
                    policy=self.policy(),
                ),
            )
            parser.assert_not_called()

    def test_rejects_corrupted_archive_member_without_library_diagnostics(self) -> None:
        corrupted = self.make_zip(
            "corrupted.zip",
            [("file.txt", b"unique-member-content")],
            compression=zipfile.ZIP_STORED,
        )
        encoded = corrupted.read_bytes()
        corrupted.write_bytes(
            encoded.replace(b"unique-member-content", b"tampered-member-bytes", 1)
        )
        self.assert_rejected(
            "archive_invalid",
            lambda: attachments.prepare_attachments(
                [attachments.AttachmentSpec(corrupted, "application/zip")],
                policy=self.policy(),
            ),
        )

    def test_failure_cleans_private_temporary_tree_after_partial_staging(self) -> None:
        valid = self.write("valid.txt", b"valid")
        invalid = self.make_zip("invalid.zip", [("../escape.txt", b"escape")])
        before = set(self.stage_parent.iterdir())
        self.assert_rejected(
            "archive_path_invalid",
            lambda: attachments.prepare_attachments(
                [
                    attachments.AttachmentSpec(valid, "text/plain"),
                    attachments.AttachmentSpec(invalid, "application/zip"),
                ],
                policy=self.policy(),
            ),
        )
        self.assertEqual(set(self.stage_parent.iterdir()), before)

    def test_rejects_unsafe_policy_roots_temp_parents_and_limits(self) -> None:
        linked_root = self.base / "linked-root"
        linked_root.symlink_to(self.allowed, target_is_directory=True)
        public_temp = self.base / "public-temp"
        public_temp.mkdir(mode=0o755)

        self.assert_rejected(
            "policy_invalid",
            lambda: attachments.AttachmentPolicy(allowed_roots=()),
        )
        self.assert_rejected(
            "policy_invalid",
            lambda: attachments.AttachmentPolicy(allowed_roots=(Path("/"),)),
        )
        self.assert_rejected(
            "policy_invalid",
            lambda: attachments.AttachmentPolicy(allowed_roots=(linked_root,)),
        )
        self.assert_rejected(
            "policy_invalid",
            lambda: attachments.AttachmentPolicy(
                allowed_roots=(self.allowed,),
                temp_parent=public_temp,
            ),
        )
        self.assert_rejected(
            "policy_invalid",
            lambda: self.policy(max_attachments=5, max_expanded_files=4),
        )
        self.assert_rejected(
            "policy_invalid",
            lambda: self.policy(max_compression_ratio=1_001),
        )
        self.assert_rejected(
            "policy_invalid",
            lambda: self.policy(max_archive_central_directory_bytes=0),
        )

    def test_source_has_no_external_control_identity_or_secret_surface(self) -> None:
        source = Path(attachments.__file__).read_text(encoding="utf-8").lower()
        for forbidden in (
            "argparse",
            "browser",
            "broker",
            "cookie",
            "http://",
            "https://",
            "os.environ",
            "requests",
            "socket",
            "subprocess",
            "sys.argv",
            "token",
            "urllib",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
