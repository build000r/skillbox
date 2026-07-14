"""Typed, non-mutating clipboard snapshot substrate for seamless remote paste."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import struct
import subprocess
import tempfile
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_DIMENSION = 32_768
DEFAULT_MAX_PIXELS = 100_000_000
IMAGE_UTIS = {
    "public.png": ("image", "image/png", ".png"),
    "public.jpeg": ("image", "image/jpeg", ".jpg"),
    "public.tiff": ("image", "image/tiff", ".tiff"),
    "public.heic": ("document", "image/heic", ".heic"),
    "public.heif": ("document", "image/heif", ".heif"),
    "com.adobe.pdf": ("document", "application/pdf", ".pdf"),
}
TEXT_UTIS = {"public.utf8-plain-text", "public.plain-text", "public.text"}
FILE_UTIS = {"public.file-url", "NSFilenamesPboardType"}
MIME_TO_UTI = {
    "image/png": "public.png",
    "image/jpeg": "public.jpeg",
    "image/tiff": "public.tiff",
    "image/heic": "public.heic",
    "image/heif": "public.heif",
    "application/pdf": "com.adobe.pdf",
}


class SnapshotError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ClipboardSnapshot:
    ok: bool
    kind: str
    change_count: int
    byte_size: int = 0
    mime: str | None = None
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    source_types: tuple[str, ...] = ()
    artifact: str | None = None
    file_count: int = 0
    file_names: tuple[str, ...] = ()
    error: dict[str, str] | None = None
    _source_paths: tuple[str, ...] = field(default=(), repr=False, compare=False)

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("_source_paths", None)
        payload["source_types"] = list(self.source_types)
        # Finder basenames can themselves contain customer, project, or secret
        # metadata. Keep them available only to the in-gesture object.
        payload["file_names"] = []
        return payload


def _magic_matches(mime: str, data: bytes) -> bool:
    if mime == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime == "image/tiff":
        return data.startswith((b"II*\x00", b"MM\x00*"))
    if mime in {"image/heic", "image/heif"}:
        return (
            len(data) >= 12
            and data[4:8] == b"ftyp"
            and data[8:12]
            in {
                b"heic",
                b"heix",
                b"hevc",
                b"hevx",
                b"heim",
                b"heis",
                b"mif1",
                b"msf1",
            }
        )
    if mime == "application/pdf":
        return data.startswith(b"%PDF-")
    return False


def _materialize(data: bytes, suffix: str, output_dir: Path) -> str:
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    fd, name = tempfile.mkstemp(prefix="snapshot-", suffix=suffix, dir=output_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        return name
    except BaseException:
        os.close(fd)
        Path(name).unlink(missing_ok=True)
        raise


def _validated_dimensions(item: dict[str, Any]) -> tuple[int, int]:
    try:
        width = int(item["width"])
        height = int(item["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotError(
            "corrupt_media", "decoded image dimensions are unavailable"
        ) from exc
    return _bounded_dimensions(width, height)


def _bounded_dimensions(width: int, height: int) -> tuple[int, int]:
    if (
        width < 1
        or height < 1
        or width > DEFAULT_MAX_DIMENSION
        or height > DEFAULT_MAX_DIMENSION
        or width * height > DEFAULT_MAX_PIXELS
    ):
        raise SnapshotError(
            "corrupt_media", "decoded image dimensions exceed the safety limit"
        )
    return width, height


def _image_dimensions(mime: str, data: bytes) -> tuple[int, int]:
    """Read dimensions from bounded image headers without decoding pixels."""
    if mime == "image/png":
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise SnapshotError("corrupt_media", "PNG is missing its IHDR")
        return struct.unpack(">II", data[16:24])
    if mime == "image/jpeg":
        position = 2
        sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while position + 4 <= len(data):
            if data[position] != 0xFF:
                position += 1
                continue
            marker = data[position + 1]
            position += 2
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if position + 2 > len(data):
                break
            length = int.from_bytes(data[position : position + 2], "big")
            if length < 2 or position + length > len(data):
                break
            if marker in sof and length >= 7:
                height = int.from_bytes(data[position + 3 : position + 5], "big")
                width = int.from_bytes(data[position + 5 : position + 7], "big")
                return width, height
            position += length
        raise SnapshotError("corrupt_media", "JPEG dimensions are unavailable")
    if mime == "image/tiff":
        endian = "<" if data.startswith(b"II*\x00") else ">"
        if len(data) < 8:
            raise SnapshotError("corrupt_media", "TIFF header is incomplete")
        ifd = struct.unpack(f"{endian}I", data[4:8])[0]
        if ifd + 2 > len(data):
            raise SnapshotError("corrupt_media", "TIFF directory is unavailable")
        count = struct.unpack(f"{endian}H", data[ifd : ifd + 2])[0]
        dimensions: dict[int, int] = {}
        for index in range(count):
            start = ifd + 2 + index * 12
            if start + 12 > len(data):
                break
            tag, value_type, value_count = struct.unpack(
                f"{endian}HHI", data[start : start + 8]
            )
            if tag not in {256, 257} or value_count != 1 or value_type not in {3, 4}:
                continue
            raw = data[start + 8 : start + 12]
            dimensions[tag] = (
                struct.unpack(f"{endian}H", raw[:2])[0]
                if value_type == 3
                else struct.unpack(f"{endian}I", raw)[0]
            )
        if 256 in dimensions and 257 in dimensions:
            return dimensions[256], dimensions[257]
        raise SnapshotError("corrupt_media", "TIFF dimensions are unavailable")
    raise SnapshotError("corrupt_media", f"dimensions unsupported for {mime}")


def snapshot_from_payload(
    payload: dict[str, Any],
    *,
    output_dir: Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> ClipboardSnapshot:
    before = int(payload.get("change_count_before", payload.get("change_count", 0)))
    after = int(payload.get("change_count_after", before))
    source_types = tuple(str(item) for item in payload.get("types", []))
    if before != after:
        raise SnapshotError(
            "clipboard_changed",
            "clipboard changed while the snapshot was being captured",
        )

    items = payload.get("items", [])
    if not isinstance(items, list):
        raise SnapshotError("invalid_payload", "items must be a list")
    by_uti = {str(item.get("uti")): item for item in items if isinstance(item, dict)}

    for uti, (kind, mime, suffix) in IMAGE_UTIS.items():
        item = by_uti.get(uti)
        if not item:
            continue
        try:
            data = base64.b64decode(str(item["data_base64"]), validate=True)
        except (KeyError, ValueError) as exc:
            raise SnapshotError("corrupt_media", f"invalid base64 for {uti}") from exc
        if len(data) > max_bytes:
            raise SnapshotError(
                "too_large", f"clipboard image exceeds {max_bytes} bytes"
            )
        if not _magic_matches(mime, data):
            raise SnapshotError("corrupt_media", f"clipboard bytes do not match {mime}")
        width: int | None = None
        height: int | None = None
        if kind == "image":
            _validated_dimensions(item)
            width, height = _bounded_dimensions(*_image_dimensions(mime, data))
        artifact = _materialize(data, suffix, output_dir) if output_dir else None
        return ClipboardSnapshot(
            ok=True,
            kind=kind,
            change_count=before,
            byte_size=len(data),
            mime=mime,
            sha256=hashlib.sha256(data).hexdigest(),
            width=width,
            height=height,
            source_types=source_types,
            artifact=artifact,
        )

    file_item = next((by_uti[uti] for uti in FILE_UTIS if uti in by_uti), None)
    if file_item:
        paths = tuple(str(value) for value in file_item.get("paths", []))
        encoded_size = sum(len(value.encode("utf-8")) for value in paths)
        if encoded_size > max_bytes:
            raise SnapshotError(
                "too_large", f"clipboard file list exceeds {max_bytes} bytes"
            )
        digest = hashlib.sha256("\0".join(paths).encode()).hexdigest()
        return ClipboardSnapshot(
            ok=True,
            kind="files",
            change_count=before,
            byte_size=encoded_size,
            mime="text/uri-list",
            sha256=digest,
            source_types=source_types,
            file_count=len(paths),
            file_names=tuple(Path(value).name for value in paths),
            _source_paths=paths,
        )

    text_item = next((by_uti[uti] for uti in TEXT_UTIS if uti in by_uti), None)
    if text_item:
        data = str(text_item.get("text", "")).encode("utf-8")
        if len(data) > max_bytes:
            raise SnapshotError(
                "too_large", f"clipboard text exceeds {max_bytes} bytes"
            )
        return ClipboardSnapshot(
            ok=True,
            kind="text",
            change_count=before,
            byte_size=len(data),
            mime="text/plain;charset=utf-8",
            sha256=hashlib.sha256(data).hexdigest(),
            source_types=source_types,
        )

    if not items and not source_types:
        return ClipboardSnapshot(ok=True, kind="empty", change_count=before)
    return ClipboardSnapshot(
        ok=False,
        kind="unsupported",
        change_count=before,
        source_types=source_types,
        error={
            "code": "unsupported_type",
            "message": "clipboard contains no supported type",
        },
    )


def error_snapshot(error: SnapshotError, change_count: int = 0) -> ClipboardSnapshot:
    return ClipboardSnapshot(
        ok=False,
        kind="error",
        change_count=change_count,
        error={"code": error.code, "message": str(error)},
    )


JXA_CAPTURE = r"""
ObjC.import('AppKit')
ObjC.import('Foundation')
function b64(data) { return ObjC.unwrap(data.base64EncodedStringWithOptions(0)) }
function present(value) { return ObjC.unwrap(value) !== undefined }
function run() {
  const pb = $.NSPasteboard.generalPasteboard
  const before = Number(pb.changeCount)
  const types = ObjC.deepUnwrap(pb.types) || []
  const items = []
  let data = pb.dataForType($.NSPasteboardTypePNG)
  if (present(data)) {
    const image = $.NSImage.alloc.initWithData(data)
    items.push({uti:'public.png', data_base64:b64(data), width:Number(image.size.width), height:Number(image.size.height)})
  } else {
    data = pb.dataForType('public.jpeg')
    if (present(data)) {
      const image = $.NSImage.alloc.initWithData(data)
      items.push({uti:'public.jpeg', data_base64:b64(data), width:Number(image.size.width), height:Number(image.size.height)})
    } else {
      data = pb.dataForType($.NSPasteboardTypeTIFF)
      if (present(data)) {
        const image = $.NSImage.alloc.initWithData(data)
        items.push({uti:'public.tiff', data_base64:b64(data), width:Number(image.size.width), height:Number(image.size.height)})
      } else {
        data = pb.dataForType('public.heic')
        if (present(data)) {
          items.push({uti:'public.heic', data_base64:b64(data)})
        } else {
          data = pb.dataForType('public.heif')
          if (present(data)) {
            items.push({uti:'public.heif', data_base64:b64(data)})
          } else {
            data = pb.dataForType('com.adobe.pdf')
            if (present(data)) items.push({uti:'com.adobe.pdf', data_base64:b64(data)})
          }
        }
      }
    }
  }
  const files = ObjC.deepUnwrap(pb.propertyListForType('NSFilenamesPboardType')) || []
  if (files.length) items.push({uti:'NSFilenamesPboardType', paths:files})
  if (!items.length) {
    const text = ObjC.unwrap(pb.stringForType($.NSPasteboardTypeString))
    if (text !== undefined && text !== null) items.push({uti:'public.utf8-plain-text', text:String(text)})
  }
  const after = Number(pb.changeCount)
  return JSON.stringify({change_count_before:before, change_count_after:after, types:types, items:items})
}
"""

JXA_CHANGE_COUNT = r"""
ObjC.import('AppKit')
function run() { return String(Number($.NSPasteboard.generalPasteboard.changeCount)) }
"""


def capture_macos_payload(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise SnapshotError(
            "unsupported_platform", "live clipboard capture requires macOS"
        )
    try:
        proc = runner(
            ["osascript", "-l", "JavaScript", "-e", JXA_CAPTURE],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError("capture_failed", "macOS clipboard capture failed") from exc
    if proc.returncode != 0:
        raise SnapshotError("capture_failed", "macOS clipboard capture failed")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SnapshotError(
            "capture_failed", "macOS clipboard returned invalid JSON"
        ) from exc


def current_change_count(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    """Read only NSPasteboard.changeCount without requesting clipboard contents."""
    if platform.system() != "Darwin":
        raise SnapshotError(
            "unsupported_platform", "live clipboard capture requires macOS"
        )
    try:
        proc = runner(
            ["osascript", "-l", "JavaScript", "-e", JXA_CHANGE_COUNT],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError(
            "capture_failed", "macOS clipboard generation check failed"
        ) from exc
    if proc.returncode != 0:
        raise SnapshotError("capture_failed", "macOS clipboard generation check failed")
    try:
        return int(proc.stdout.strip())
    except ValueError as exc:
        raise SnapshotError(
            "capture_failed", "macOS clipboard returned an invalid generation"
        ) from exc


def _payload_generation(types: list[str], items: list[dict[str, Any]]) -> int:
    canonical = json.dumps(
        {"types": types, "items": items},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return int.from_bytes(hashlib.sha256(canonical).digest()[:8], "big")


def _portable_payload(
    types: list[str], items: list[dict[str, Any]]
) -> dict[str, Any]:
    generation = _payload_generation(types, items)
    return {
        "change_count_before": generation,
        "change_count_after": generation,
        "types": types,
        "items": items,
    }


def _clipboard_command(
    command: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]],
) -> bytes:
    try:
        result = runner(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError("capture_failed", "operator clipboard capture failed") from exc
    if result.returncode != 0:
        raise SnapshotError("capture_failed", "operator clipboard capture failed")
    return result.stdout


def _decode_clipboard_text(data: bytes) -> str:
    try:
        return data.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise SnapshotError(
            "corrupt_media", "clipboard text is not valid UTF-8"
        ) from exc


def capture_linux_payload(
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> dict[str, Any]:
    """Capture Wayland or X11 clipboard data inside one explicit paste gesture."""
    if platform.system() != "Linux":
        raise SnapshotError("unsupported_platform", "Linux clipboard is unavailable")
    backend = "wl-paste"
    try:
        types_raw = _clipboard_command(["wl-paste", "--list-types"], runner=runner)
    except SnapshotError:
        backend = "xclip"
        types_raw = _clipboard_command(
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            runner=runner,
        )
    types = [
        line.strip()
        for line in types_raw.decode("utf-8", "replace").splitlines()
        if line.strip()
    ]

    def read_type(mime: str) -> bytes:
        command = (
            ["wl-paste", "--no-newline", "--type", mime]
            if backend == "wl-paste"
            else ["xclip", "-selection", "clipboard", "-t", mime, "-o"]
        )
        return _clipboard_command(command, runner=runner)

    for mime, uti in MIME_TO_UTI.items():
        if mime not in types:
            continue
        data = read_type(mime)
        item: dict[str, Any] = {
            "uti": uti,
            "data_base64": base64.b64encode(data).decode("ascii"),
        }
        if mime in {"image/png", "image/jpeg", "image/tiff"}:
            width, height = _image_dimensions(mime, data)
            item.update(width=width, height=height)
        return _portable_payload(types, [item])
    if "text/uri-list" in types:
        paths: list[str] = []
        for raw in _decode_clipboard_text(read_type("text/uri-list")).splitlines():
            if not raw or raw.startswith("#"):
                continue
            parsed = urllib.parse.urlparse(raw)
            if parsed.scheme == "file" and not parsed.netloc:
                paths.append(urllib.parse.unquote(parsed.path))
        return _portable_payload(types, [{"uti": "public.file-url", "paths": paths}])
    text_type = next(
        (
            item
            for item in types
            if item.startswith("text/plain") or item in {"UTF8_STRING", "STRING"}
        ),
        None,
    )
    if text_type:
        text = _decode_clipboard_text(read_type(text_type))
        return _portable_payload(
            types, [{"uti": "public.utf8-plain-text", "text": text}]
        )
    return _portable_payload(types, [])


POWERSHELL_CAPTURE = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$types = @()
$items = @()
if ([Windows.Forms.Clipboard]::ContainsImage()) {
  $stream = New-Object IO.MemoryStream
  $image = [Windows.Forms.Clipboard]::GetImage()
  $image.Save($stream, [Drawing.Imaging.ImageFormat]::Png)
  $types += 'image/png'
  $items += @{uti='public.png'; data_base64=[Convert]::ToBase64String($stream.ToArray()); width=$image.Width; height=$image.Height}
} elseif ([Windows.Forms.Clipboard]::ContainsFileDropList()) {
  $paths = @([Windows.Forms.Clipboard]::GetFileDropList())
  $types += 'text/uri-list'
  $items += @{uti='public.file-url'; paths=$paths}
} elseif ([Windows.Forms.Clipboard]::ContainsText()) {
  $types += 'text/plain;charset=utf-8'
  $items += @{uti='public.utf8-plain-text'; text=[Windows.Forms.Clipboard]::GetText()}
}
@{types=$types; items=$items} | ConvertTo-Json -Compress -Depth 5
"""


def capture_windows_payload(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Capture Windows clipboard data from an STA PowerShell process."""
    if platform.system() != "Windows":
        raise SnapshotError("unsupported_platform", "Windows clipboard is unavailable")
    try:
        result = runner(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-STA",
                "-Command",
                POWERSHELL_CAPTURE,
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError("capture_failed", "Windows clipboard capture failed") from exc
    if result.returncode != 0:
        raise SnapshotError("capture_failed", "Windows clipboard capture failed")
    try:
        payload = json.loads(result.stdout)
        raw_types = payload.get("types", [])
        raw_items = payload.get("items", [])
        if isinstance(raw_types, str):
            raw_types = [raw_types]
        if isinstance(raw_items, dict):
            raw_items = [raw_items]
        types = [str(value) for value in raw_types]
        items = [value for value in raw_items if isinstance(value, dict)]
    except (AttributeError, json.JSONDecodeError) as exc:
        raise SnapshotError("capture_failed", "Windows clipboard returned invalid JSON") from exc
    return _portable_payload(types, items)


def capture_operator_payload() -> dict[str, Any]:
    system = platform.system()
    if system == "Darwin":
        return capture_macos_payload()
    if system == "Linux":
        return capture_linux_payload()
    if system == "Windows":
        return capture_windows_payload()
    raise SnapshotError("unsupported_platform", f"unsupported operator platform: {system}")


def current_operator_generation() -> int:
    if platform.system() == "Darwin":
        return current_change_count()
    # Linux and Windows expose no portable monotonic pasteboard generation.
    # Re-reading inside the already-authorized gesture yields a content digest
    # generation and catches clipboard replacement before injection.
    return int(capture_operator_payload()["change_count_before"])
