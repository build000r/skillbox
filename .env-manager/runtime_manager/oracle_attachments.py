from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import struct
import tempfile
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Iterable, Iterator, NoReturn


DEFAULT_ALLOWED_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/pdf",
        "application/zip",
        "image/jpeg",
        "image/png",
        "text/csv",
        "text/markdown",
        "text/plain",
    }
)

_MIME_BY_SUFFIX = {
    ".csv": "text/csv",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".markdown": "text/markdown",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".txt": "text/plain",
    ".zip": "application/zip",
}
_STAGED_SUFFIX = {
    "application/json": ".json",
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "text/csv": ".csv",
    "text/markdown": ".md",
    "text/plain": ".txt",
}
_TEXT_MIME_TYPES = frozenset(
    {"application/json", "text/csv", "text/markdown", "text/plain"}
)
_READ_CHUNK_BYTES = 64 * 1024
_MAX_ARCHIVE_NAME_BYTES = 512
_ERROR_MESSAGE = "oracle attachment validation rejected"
_ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP_CENTRAL_FILE_SIGNATURE = b"PK\x01\x02"
_ZIP_EOCD = struct.Struct("<4s4H2LH")
_ZIP64_EOCD = struct.Struct("<4sQ2H2L4Q")
_ZIP64_LOCATOR = struct.Struct("<4sLQL")
_ZIP_CENTRAL_FILE_FIXED_BYTES = 46
_ZIP_MAX_COMMENT_BYTES = (1 << 16) - 1
_ZIP16_SENTINEL = (1 << 16) - 1
_ZIP32_SENTINEL = (1 << 32) - 1


class AttachmentValidationError(ValueError):
    """A fail-closed attachment rejection with a non-sensitive reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(_ERROR_MESSAGE)
        self.code = code


def _reject(code: str) -> NoReturn:
    raise AttachmentValidationError(code) from None


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _absolute_normalized_path(raw: object, code: str) -> Path:
    try:
        rendered = os.fspath(raw)
    except TypeError:
        _reject(code)
    if (
        not isinstance(rendered, str)
        or not rendered
        or "\x00" in rendered
        or "\n" in rendered
        or "\r" in rendered
    ):
        _reject(code)
    path = Path(rendered)
    if not path.is_absolute() or os.path.normpath(rendered) != rendered:
        _reject(code)
    return path


def _canonical_directory(
    raw: object,
    code: str,
    *,
    private: bool = False,
    reject_filesystem_root: bool = False,
) -> Path:
    path = _absolute_normalized_path(raw, code)
    if reject_filesystem_root and path == Path(path.anchor):
        _reject(code)
    try:
        metadata = os.lstat(path)
        resolved = Path(os.path.realpath(path))
    except OSError:
        _reject(code)
    if (
        resolved != path
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        _reject(code)
    if private:
        if metadata.st_mode & 0o077:
            _reject(code)
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            _reject(code)
    return path


@dataclass(frozen=True)
class AttachmentSpec:
    path: Path
    mime_type: str

    def __post_init__(self) -> None:
        try:
            rendered = os.fspath(self.path)
        except TypeError:
            _reject("attachment_invalid")
        if not isinstance(rendered, str):
            _reject("attachment_invalid")
        if (
            not isinstance(self.mime_type, str)
            or not self.mime_type
            or self.mime_type != self.mime_type.strip().lower()
            or ";" in self.mime_type
        ):
            _reject("attachment_invalid")
        object.__setattr__(self, "path", Path(rendered))


@dataclass(frozen=True)
class AttachmentPolicy:
    allowed_roots: tuple[Path, ...]
    allowed_mime_types: frozenset[str] = DEFAULT_ALLOWED_MIME_TYPES
    max_attachments: int = 8
    max_source_bytes: int = 16 * 1024 * 1024
    max_total_source_bytes: int = 32 * 1024 * 1024
    max_expanded_files: int = 32
    max_archive_entries: int = 64
    max_archive_member_bytes: int = 8 * 1024 * 1024
    max_total_expanded_bytes: int = 64 * 1024 * 1024
    max_compression_ratio: int = 100
    temp_parent: Path | None = None
    max_archive_central_directory_bytes: int = 256 * 1024

    def __post_init__(self) -> None:
        try:
            roots = tuple(self.allowed_roots)
        except TypeError:
            _reject("policy_invalid")
        if not roots:
            _reject("policy_invalid")
        canonical_roots = tuple(
            _canonical_directory(
                root,
                "policy_invalid",
                reject_filesystem_root=True,
            )
            for root in roots
        )
        root_keys = {
            unicodedata.normalize("NFC", str(root)).casefold()
            for root in canonical_roots
        }
        if len(root_keys) != len(canonical_roots):
            _reject("policy_invalid")

        if not isinstance(self.allowed_mime_types, frozenset):
            _reject("policy_invalid")
        if not self.allowed_mime_types or not self.allowed_mime_types.issubset(
            DEFAULT_ALLOWED_MIME_TYPES
        ):
            _reject("policy_invalid")

        numeric_fields = (
            self.max_attachments,
            self.max_source_bytes,
            self.max_total_source_bytes,
            self.max_expanded_files,
            self.max_archive_entries,
            self.max_archive_central_directory_bytes,
            self.max_archive_member_bytes,
            self.max_total_expanded_bytes,
            self.max_compression_ratio,
        )
        if not all(_positive_integer(value) for value in numeric_fields):
            _reject("policy_invalid")
        if (
            self.max_source_bytes > self.max_total_source_bytes
            or self.max_archive_member_bytes > self.max_total_expanded_bytes
            or self.max_attachments > self.max_expanded_files
            or self.max_compression_ratio > 1_000
        ):
            _reject("policy_invalid")

        temp_parent = self.temp_parent
        if temp_parent is not None:
            temp_parent = _canonical_directory(
                temp_parent,
                "policy_invalid",
                private=True,
                reject_filesystem_root=True,
            )

        object.__setattr__(self, "allowed_roots", canonical_roots)
        object.__setattr__(self, "temp_parent", temp_parent)


@dataclass(frozen=True)
class PreparedAttachment:
    staged_path: Path
    display_name: str
    mime_type: str
    bytes: int
    sha256: str


class PreparedAttachmentBatch:
    """Context-managed private copies that become unusable after cleanup."""

    __slots__ = (
        "_attachments",
        "_closed",
        "_expanded_bytes",
        "_root",
        "_source_bytes",
        "_source_count",
        "_temporary",
    )

    def __init__(
        self,
        temporary: tempfile.TemporaryDirectory[str],
        attachments: tuple[PreparedAttachment, ...],
        *,
        source_count: int,
        source_bytes: int,
        expanded_bytes: int,
    ) -> None:
        self._temporary = temporary
        self._root = Path(os.path.realpath(temporary.name))
        self._attachments = attachments
        self._source_count = source_count
        self._source_bytes = source_bytes
        self._expanded_bytes = expanded_bytes
        self._closed = False

    @property
    def root(self) -> Path:
        return self._root

    @property
    def attachments(self) -> tuple[PreparedAttachment, ...]:
        return self._attachments

    @property
    def source_count(self) -> int:
        return self._source_count

    @property
    def source_bytes(self) -> int:
        return self._source_bytes

    @property
    def expanded_bytes(self) -> int:
        return self._expanded_bytes

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> PreparedAttachmentBatch:
        if self._closed:
            _reject("batch_closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._temporary.cleanup()
        except OSError:
            _reject("temp_cleanup_failed")
        self._closed = True


@dataclass
class _BuildState:
    root: Path
    policy: AttachmentPolicy
    attachments: list[PreparedAttachment]
    source_bytes: int = 0
    expanded_bytes: int = 0


def _path_is_allowed(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _validate_source_path(path: Path, policy: AttachmentPolicy) -> os.stat_result:
    path = _absolute_normalized_path(path, "path_not_canonical")
    try:
        metadata = os.lstat(path)
        resolved = Path(os.path.realpath(path))
    except OSError:
        _reject("path_invalid")
    if resolved != path:
        _reject("path_not_canonical")
    if not _path_is_allowed(path, policy.allowed_roots):
        _reject("path_not_allowed")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        _reject("source_not_regular")
    if metadata.st_nlink != 1:
        _reject("source_hardlinked")
    if metadata.st_size < 1:
        _reject("source_empty")
    if metadata.st_size > policy.max_source_bytes:
        _reject("source_bytes_exceeded")
    return metadata


def _metadata_fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_source(
    path: Path,
    policy: AttachmentPolicy,
) -> tuple[bytes, tuple[int, int]]:
    initial = _validate_source_path(path, policy)
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _reject("path_invalid")
    try:
        opened = os.fstat(descriptor)
        if _metadata_fingerprint(initial) != _metadata_fingerprint(opened):
            _reject("source_changed")
        if not stat.S_ISREG(opened.st_mode):
            _reject("source_not_regular")
        if opened.st_nlink != 1:
            _reject("source_hardlinked")

        chunks: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            observed += len(chunk)
            if observed > policy.max_source_bytes:
                _reject("source_bytes_exceeded")
            chunks.append(chunk)
        after_read = os.fstat(descriptor)
        if _metadata_fingerprint(opened) != _metadata_fingerprint(after_read):
            _reject("source_changed")
    except AttachmentValidationError:
        raise
    except OSError:
        _reject("source_changed")
    finally:
        os.close(descriptor)

    try:
        final = os.lstat(path)
        final_resolved = Path(os.path.realpath(path))
    except OSError:
        _reject("source_changed")
    if (
        _metadata_fingerprint(after_read) != _metadata_fingerprint(final)
        or final_resolved != path
        or not _path_is_allowed(final_resolved, policy.allowed_roots)
    ):
        _reject("source_changed")

    data = b"".join(chunks)
    if len(data) != opened.st_size or not data:
        _reject("source_changed")
    return data, (opened.st_dev, opened.st_ino)


def _mime_for_name(name: str) -> str:
    suffix = PurePosixPath(name).suffix.lower()
    mime_type = _MIME_BY_SUFFIX.get(suffix)
    if mime_type is None:
        _reject("mime_unsupported")
    return mime_type


def _known_magic_mime(data: bytes) -> str | None:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "application/zip"
    return None


def _validate_content(data: bytes, mime_type: str) -> None:
    magic_mime = _known_magic_mime(data)
    if magic_mime is not None and magic_mime != mime_type:
        _reject("mime_content_mismatch")
    if mime_type in _TEXT_MIME_TYPES:
        if b"\x00" in data:
            _reject("mime_content_mismatch")
        try:
            rendered = data.decode("utf-8")
        except UnicodeDecodeError:
            _reject("mime_content_mismatch")
        if mime_type == "application/json":
            try:
                json.loads(rendered)
            except (ValueError, RecursionError):
                _reject("mime_content_mismatch")
    elif mime_type == "application/pdf":
        if not data.startswith(b"%PDF-"):
            _reject("mime_content_mismatch")
    elif mime_type == "image/png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            _reject("mime_content_mismatch")
    elif mime_type == "image/jpeg":
        if not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
            _reject("mime_content_mismatch")
    elif mime_type == "application/zip":
        if _known_magic_mime(data) != "application/zip":
            _reject("mime_content_mismatch")
    else:
        _reject("mime_unsupported")


def _assert_private_temp_root(path: Path) -> None:
    try:
        metadata = os.lstat(path)
        resolved = Path(os.path.realpath(path))
    except OSError:
        _reject("temp_invalid")
    if (
        resolved != path
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_mode & 0o077
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        _reject("temp_invalid")


def _write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            _reject("stage_write_failed")
        remaining = remaining[written:]


def _stage_bytes(state: _BuildState, data: bytes, mime_type: str) -> None:
    next_count = len(state.attachments) + 1
    if next_count > state.policy.max_expanded_files:
        _reject("expanded_file_count_exceeded")
    next_total = state.expanded_bytes + len(data)
    if next_total > state.policy.max_total_expanded_bytes:
        _reject("expanded_bytes_exceeded")

    suffix = _STAGED_SUFFIX.get(mime_type)
    if suffix is None:
        _reject("nested_archive_forbidden")
    display_name = f"attachment-{next_count:03d}{suffix}"
    staged_path = state.root / display_name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(staged_path, flags, 0o600)
        try:
            _write_all(descriptor, data)
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size != len(data)
                or metadata.st_mode & 0o077
            ):
                _reject("stage_write_failed")
        finally:
            os.close(descriptor)
    except AttachmentValidationError:
        raise
    except OSError:
        _reject("stage_write_failed")

    try:
        final = os.lstat(staged_path)
        resolved = Path(os.path.realpath(staged_path))
    except OSError:
        _reject("stage_write_failed")
    if (
        resolved != staged_path
        or final.st_nlink != 1
        or not stat.S_ISREG(final.st_mode)
        or final.st_mode & 0o077
    ):
        _reject("stage_write_failed")

    state.attachments.append(
        PreparedAttachment(
            staged_path=staged_path,
            display_name=display_name,
            mime_type=mime_type,
            bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
    )
    state.expanded_bytes = next_total


def _archive_member_key(info: zipfile.ZipInfo) -> tuple[str, bool]:
    original_name = getattr(info, "orig_filename", info.filename)
    if original_name != info.filename:
        _reject("archive_path_invalid")
    name = info.filename
    is_directory = info.is_dir()
    canonical_name = name[:-1] if is_directory and name.endswith("/") else name
    if (
        not canonical_name
        or name.startswith("/")
        or "\\" in name
        or ":" in name
        or "\x00" in name
        or len(name.encode("utf-8")) > _MAX_ARCHIVE_NAME_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        _reject("archive_path_invalid")
    member_path = PurePosixPath(canonical_name)
    if (
        member_path.is_absolute()
        or any(part in {"", ".", ".."} for part in member_path.parts)
        or member_path.as_posix() != canonical_name
        or unicodedata.normalize("NFC", canonical_name) != canonical_name
    ):
        _reject("archive_path_invalid")
    return canonical_name.casefold(), is_directory


def _validate_archive_member_type(info: zipfile.ZipInfo, is_directory: bool) -> None:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if is_directory:
        if file_type not in {0, stat.S_IFDIR}:
            _reject("archive_unsafe_type")
    elif file_type not in {0, stat.S_IFREG}:
        _reject("archive_unsafe_type")


def _find_archive_eocd(data: bytes) -> int:
    if len(data) < _ZIP_EOCD.size:
        _reject("archive_invalid")
    search_start = max(
        0,
        len(data) - _ZIP_EOCD.size - _ZIP_MAX_COMMENT_BYTES,
    )
    for offset in range(len(data) - _ZIP_EOCD.size, search_start - 1, -1):
        if not data.startswith(_ZIP_EOCD_SIGNATURE, offset):
            continue
        comment_bytes = struct.unpack_from("<H", data, offset + 20)[0]
        if offset + _ZIP_EOCD.size + comment_bytes == len(data):
            return offset
    _reject("archive_invalid")


def _zip64_directory_metadata(
    data: bytes,
    eocd_offset: int,
    *,
    disk_number: int,
    central_disk: int,
    entries_on_disk: int,
    entry_count: int,
    central_bytes: int,
    central_offset: int,
) -> tuple[int, int, int, int]:
    locator_offset = eocd_offset - _ZIP64_LOCATOR.size
    if locator_offset < 0:
        _reject("archive_invalid")
    try:
        (
            locator_signature,
            locator_disk,
            zip64_offset,
            total_disks,
        ) = _ZIP64_LOCATOR.unpack_from(data, locator_offset)
    except struct.error:
        _reject("archive_invalid")
    if (
        locator_signature != _ZIP64_LOCATOR_SIGNATURE
        or locator_disk != 0
        or total_disks != 1
        or zip64_offset < 0
        or zip64_offset + _ZIP64_EOCD.size > locator_offset
    ):
        _reject("archive_invalid")
    try:
        (
            zip64_signature,
            record_bytes,
            _version_made,
            _version_needed,
            zip64_disk_number,
            zip64_central_disk,
            zip64_entries_on_disk,
            zip64_entry_count,
            zip64_central_bytes,
            zip64_central_offset,
        ) = _ZIP64_EOCD.unpack_from(data, zip64_offset)
    except struct.error:
        _reject("archive_invalid")
    if (
        zip64_signature != _ZIP64_EOCD_SIGNATURE
        or record_bytes < _ZIP64_EOCD.size - 12
        or zip64_offset + 12 + record_bytes != locator_offset
        or zip64_disk_number != 0
        or zip64_central_disk != 0
        or zip64_entries_on_disk != zip64_entry_count
    ):
        _reject("archive_invalid")
    bindings = (
        (disk_number, _ZIP16_SENTINEL, zip64_disk_number),
        (central_disk, _ZIP16_SENTINEL, zip64_central_disk),
        (entries_on_disk, _ZIP16_SENTINEL, zip64_entries_on_disk),
        (entry_count, _ZIP16_SENTINEL, zip64_entry_count),
        (central_bytes, _ZIP32_SENTINEL, zip64_central_bytes),
        (central_offset, _ZIP32_SENTINEL, zip64_central_offset),
    )
    if any(
        standard != sentinel and standard != extended
        for standard, sentinel, extended in bindings
    ):
        _reject("archive_invalid")
    return (
        zip64_entry_count,
        zip64_central_bytes,
        zip64_central_offset,
        zip64_offset,
    )


def _preflight_archive(data: bytes, policy: AttachmentPolicy) -> None:
    """Bound ZIP metadata before ZipFile can allocate per-entry objects."""

    eocd_offset = _find_archive_eocd(data)
    try:
        (
            signature,
            disk_number,
            central_disk,
            entries_on_disk,
            entry_count,
            central_bytes,
            central_offset,
            _comment_bytes,
        ) = _ZIP_EOCD.unpack_from(data, eocd_offset)
    except struct.error:
        _reject("archive_invalid")
    if signature != _ZIP_EOCD_SIGNATURE:
        _reject("archive_invalid")

    requires_zip64 = (
        disk_number == _ZIP16_SENTINEL
        or central_disk == _ZIP16_SENTINEL
        or entries_on_disk == _ZIP16_SENTINEL
        or entry_count == _ZIP16_SENTINEL
        or central_bytes == _ZIP32_SENTINEL
        or central_offset == _ZIP32_SENTINEL
    )
    if requires_zip64:
        (
            entry_count,
            central_bytes,
            central_offset,
            directory_end,
        ) = _zip64_directory_metadata(
            data,
            eocd_offset,
            disk_number=disk_number,
            central_disk=central_disk,
            entries_on_disk=entries_on_disk,
            entry_count=entry_count,
            central_bytes=central_bytes,
            central_offset=central_offset,
        )
    else:
        if disk_number != 0 or central_disk != 0 or entries_on_disk != entry_count:
            _reject("archive_invalid")
        directory_end = eocd_offset

    if entry_count < 1:
        _reject("archive_empty")
    if entry_count > policy.max_archive_entries:
        _reject("archive_entries_exceeded")
    if central_bytes > policy.max_archive_central_directory_bytes:
        _reject("archive_central_directory_exceeded")
    if central_bytes < entry_count * _ZIP_CENTRAL_FILE_FIXED_BYTES:
        _reject("archive_invalid")
    central_end = central_offset + central_bytes
    if central_offset < 0 or central_end != directory_end or central_end > len(data):
        _reject("archive_invalid")

    cursor = central_offset
    observed_entries = 0
    while cursor < central_end:
        if observed_entries >= policy.max_archive_entries:
            _reject("archive_entries_exceeded")
        if central_end - cursor < _ZIP_CENTRAL_FILE_FIXED_BYTES or not data.startswith(
            _ZIP_CENTRAL_FILE_SIGNATURE, cursor
        ):
            _reject("archive_invalid")
        flag_bits = struct.unpack_from("<H", data, cursor + 8)[0]
        name_bytes, extra_bytes, comment_bytes = struct.unpack_from(
            "<3H",
            data,
            cursor + 28,
        )
        next_cursor = (
            cursor
            + _ZIP_CENTRAL_FILE_FIXED_BYTES
            + name_bytes
            + extra_bytes
            + comment_bytes
        )
        if next_cursor > central_end:
            _reject("archive_invalid")
        if flag_bits & 0x1:
            _reject("archive_encrypted")
        observed_entries += 1
        cursor = next_cursor
    if cursor != central_end or observed_entries != entry_count:
        _reject("archive_invalid")


def _safe_archive_members(
    archive: zipfile.ZipFile,
    state: _BuildState,
) -> list[tuple[zipfile.ZipInfo, str]]:
    members = archive.infolist()
    if not members:
        _reject("archive_empty")
    if len(members) > state.policy.max_archive_entries:
        _reject("archive_entries_exceeded")

    seen: dict[str, bool] = {}
    safe_files: list[tuple[zipfile.ZipInfo, str]] = []
    declared_bytes = 0
    for info in members:
        key, is_directory = _archive_member_key(info)
        if key in seen:
            _reject("archive_duplicate_path")
        for previous, previous_is_directory in seen.items():
            if key.startswith(f"{previous}/") and not previous_is_directory:
                _reject("archive_duplicate_path")
            if previous.startswith(f"{key}/") and not is_directory:
                _reject("archive_duplicate_path")
        seen[key] = is_directory
        _validate_archive_member_type(info, is_directory)
        if info.flag_bits & 0x1:
            _reject("archive_encrypted")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            _reject("archive_compression_unsupported")
        if is_directory:
            continue
        if info.file_size < 1:
            _reject("archive_member_empty")
        if info.file_size > state.policy.max_archive_member_bytes:
            _reject("archive_member_bytes_exceeded")
        if (
            info.compress_size < 1
            or info.file_size > info.compress_size * state.policy.max_compression_ratio
        ):
            _reject("archive_compression_ratio_exceeded")
        mime_type = _mime_for_name(info.filename)
        if mime_type == "application/zip":
            _reject("nested_archive_forbidden")
        if mime_type not in state.policy.allowed_mime_types:
            _reject("mime_not_allowed")
        declared_bytes += info.file_size
        safe_files.append((info, mime_type))

    if not safe_files:
        _reject("archive_empty")
    if len(state.attachments) + len(safe_files) > state.policy.max_expanded_files:
        _reject("expanded_file_count_exceeded")
    if state.expanded_bytes + declared_bytes > state.policy.max_total_expanded_bytes:
        _reject("expanded_bytes_exceeded")
    return safe_files


def _read_archive_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    policy: AttachmentPolicy,
) -> bytes:
    chunks: list[bytes] = []
    observed = 0
    try:
        with archive.open(info, "r") as member:
            while True:
                chunk = member.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                observed += len(chunk)
                if observed > policy.max_archive_member_bytes:
                    _reject("archive_member_bytes_exceeded")
                chunks.append(chunk)
    except AttachmentValidationError:
        raise
    except (
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
        UnicodeError,
        ValueError,
        zipfile.BadZipFile,
        zlib.error,
    ):
        _reject("archive_invalid")
    data = b"".join(chunks)
    if len(data) != info.file_size:
        _reject("archive_invalid")
    return data


def _expand_archive(state: _BuildState, data: bytes) -> None:
    _preflight_archive(data, state.policy)
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
            safe_files = _safe_archive_members(archive, state)
            for info, mime_type in safe_files:
                member_data = _read_archive_member(archive, info, state.policy)
                _validate_content(member_data, mime_type)
                _stage_bytes(state, member_data, mime_type)
    except AttachmentValidationError:
        raise
    except (
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
        UnicodeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
    ):
        _reject("archive_invalid")


def _new_private_temporary(
    policy: AttachmentPolicy,
) -> tempfile.TemporaryDirectory[str]:
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        temporary = tempfile.TemporaryDirectory(
            prefix="skillbox-oracle-attachments-",
            dir=str(policy.temp_parent) if policy.temp_parent is not None else None,
        )
        root = Path(os.path.realpath(temporary.name))
        os.chmod(root, 0o700)
        _assert_private_temp_root(root)
        return temporary
    except BaseException as error:
        if temporary is not None:
            try:
                temporary.cleanup()
            except OSError:
                raise AttachmentValidationError("temp_cleanup_failed") from error
        if isinstance(error, AttachmentValidationError):
            raise
        if isinstance(error, OSError):
            _reject("temp_invalid")
        raise


def _close_attachment_iterator(
    iterator: Iterator[AttachmentSpec],
) -> None:
    try:
        close = getattr(iterator, "close", None)
    except BaseException:
        return
    if not callable(close):
        return
    try:
        close()
    except BaseException:
        return


def _next_attachment(
    iterator: Iterator[AttachmentSpec],
) -> tuple[bool | None, object | None]:
    try:
        return True, next(iterator)
    except StopIteration:
        return False, None
    except BaseException:
        return None, None


def _bounded_attachment_specs(
    attachments: Iterable[AttachmentSpec],
    max_attachments: int,
) -> tuple[AttachmentSpec, ...]:
    """Consume at most one item beyond the configured attachment limit."""

    try:
        iterator = iter(attachments)
    except BaseException:
        iterator = None
    if iterator is None:
        _reject("attachment_list_invalid")

    specs: list[object] = []
    for _ in range(max_attachments + 1):
        status, spec = _next_attachment(iterator)
        if status is False:
            validated_specs: list[AttachmentSpec] = []
            for item in specs:
                if not isinstance(item, AttachmentSpec):
                    _reject("attachment_invalid")
                validated_specs.append(item)
            return tuple(validated_specs)
        if status is None:
            _close_attachment_iterator(iterator)
            _reject("attachment_list_invalid")
        specs.append(spec)

    _close_attachment_iterator(iterator)
    _reject("attachment_count_exceeded")


def prepare_attachments(
    attachments: Iterable[AttachmentSpec],
    *,
    policy: AttachmentPolicy,
) -> PreparedAttachmentBatch:
    """Validate and privately stage attachments without invoking external services."""

    if not isinstance(policy, AttachmentPolicy):
        _reject("policy_invalid")
    if isinstance(attachments, (str, bytes, os.PathLike)):
        _reject("attachment_list_invalid")
    specs = _bounded_attachment_specs(attachments, policy.max_attachments)

    temporary = _new_private_temporary(policy)
    root = Path(os.path.realpath(temporary.name))
    state = _BuildState(root=root, policy=policy, attachments=[])
    seen_paths: set[str] = set()
    seen_files: set[tuple[int, int]] = set()
    try:
        for spec in specs:
            path = _absolute_normalized_path(spec.path, "path_not_canonical")
            path_key = unicodedata.normalize("NFC", str(path)).casefold()
            if path_key in seen_paths:
                _reject("duplicate_source")
            seen_paths.add(path_key)

            if spec.mime_type not in policy.allowed_mime_types:
                _reject("mime_not_allowed")
            expected_mime = _mime_for_name(path.name)
            if expected_mime != spec.mime_type:
                _reject("mime_mismatch")

            data, file_identity = _read_source(path, policy)
            if file_identity in seen_files:
                _reject("duplicate_source")
            seen_files.add(file_identity)
            state.source_bytes += len(data)
            if state.source_bytes > policy.max_total_source_bytes:
                _reject("total_source_bytes_exceeded")
            _validate_content(data, spec.mime_type)
            if spec.mime_type == "application/zip":
                _expand_archive(state, data)
            else:
                _stage_bytes(state, data, spec.mime_type)

        return PreparedAttachmentBatch(
            temporary,
            tuple(state.attachments),
            source_count=len(specs),
            source_bytes=state.source_bytes,
            expanded_bytes=state.expanded_bytes,
        )
    except BaseException as error:
        try:
            temporary.cleanup()
        except OSError:
            raise AttachmentValidationError("temp_cleanup_failed") from error
        raise
