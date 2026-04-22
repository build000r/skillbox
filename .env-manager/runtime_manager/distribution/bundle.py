"""Bundle format primitives for skillbox skill distribution.

Bundle layout::

    <skill>-v<version>.skillbundle.tar.gz
    ├── SKILL.md
    ├── references/...
    └── .skill-meta/
        ├── manifest.json
        ├── signature.json
        ├── changelog.md
        └── compatibility.json

Inner manifest.json: {name, version, tree_sha256, min_skillbox_version, tags, files}.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SKILL_META_DIR = ".skill-meta"
MANIFEST_FILENAME = "manifest.json"
BUNDLE_SUFFIX = ".skillbundle.tar.gz"

_TAR_MTIME = 0
_TAR_UID = 0
_TAR_GID = 0
_TAR_UNAME = ""
_TAR_GNAME = ""


class BundleError(Exception):
    pass


class BundleStructureError(BundleError):
    pass


class BundleContentMismatchError(BundleError):
    pass


@dataclass
class BundleFileEntry:
    path: str
    sha256: str


@dataclass
class BundleManifest:
    name: str
    version: int
    tree_sha256: str
    min_skillbox_version: str
    tags: list[str]
    files: list[BundleFileEntry]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "tree_sha256": self.tree_sha256,
            "min_skillbox_version": self.min_skillbox_version,
            "tags": list(self.tags),
            "files": [{"path": f.path, "sha256": f.sha256} for f in self.files],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BundleManifest:
        required = ("name", "version", "tree_sha256", "min_skillbox_version", "tags", "files")
        missing = [k for k in required if k not in data]
        if missing:
            raise BundleStructureError(
                f"manifest missing required fields: {', '.join(missing)}"
            )
        try:
            files = [
                BundleFileEntry(path=f["path"], sha256=f["sha256"])
                for f in data["files"]
            ]
        except (KeyError, TypeError) as exc:
            raise BundleStructureError(f"malformed files array in manifest: {exc}") from exc
        return cls(
            name=str(data["name"]),
            version=int(data["version"]),
            tree_sha256=str(data["tree_sha256"]),
            min_skillbox_version=str(data["min_skillbox_version"]),
            tags=[str(t) for t in data["tags"]],
            files=files,
        )


# ---------------------------------------------------------------------------
# Hashing — mirrors shared.py:file_sha256 / tree_hash for consistency
# ---------------------------------------------------------------------------

def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _tree_hash(entries: list[tuple[str, str]]) -> str:
    hasher = hashlib.sha256()
    for rel_path, digest in sorted(entries):
        hasher.update(rel_path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(digest.encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _collect_content_files(root: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(root).as_posix()
        if rel == SKILL_META_DIR or rel.startswith(SKILL_META_DIR + "/"):
            continue
        entries.append((rel, _file_sha256(file_path)))
    return entries


def compute_tree_sha256(src_dir: Path) -> str:
    return _tree_hash(_collect_content_files(src_dir))


# ---------------------------------------------------------------------------
# Deterministic tar helpers
# ---------------------------------------------------------------------------

def _make_tarinfo(
    name: str,
    size: int = 0,
    *,
    is_dir: bool = False,
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.mtime = _TAR_MTIME
    info.uid = _TAR_UID
    info.gid = _TAR_GID
    info.uname = _TAR_UNAME
    info.gname = _TAR_GNAME
    if is_dir:
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
    else:
        info.type = tarfile.REGTYPE
        info.mode = 0o644
        info.size = size
    return info


def _unique_dirs(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for p in paths:
        parts = Path(p).parts
        for i in range(1, len(parts)):
            d = "/".join(parts[:i])
            if d not in seen:
                seen.add(d)
                result.append(d)
    result.sort()
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def pack_skill_bundle(
    src_dir: Path,
    version: int,
    *,
    name: str | None = None,
    min_skillbox_version: str = "0.0.0",
    tags: list[str] | None = None,
    output_dir: Path | None = None,
) -> Path:
    src_dir = src_dir.resolve()
    if not src_dir.is_dir():
        raise BundleError(f"source directory does not exist: {src_dir}")

    skill_name = name or src_dir.name
    content_entries = _collect_content_files(src_dir)
    tree_sha = _tree_hash(content_entries)

    manifest = BundleManifest(
        name=skill_name,
        version=version,
        tree_sha256=tree_sha,
        min_skillbox_version=min_skillbox_version,
        tags=tags or [],
        files=[BundleFileEntry(path=r, sha256=s) for r, s in content_entries],
    )
    manifest_json = json.dumps(
        manifest.to_dict(), indent=2, sort_keys=True,
    ).encode("utf-8")

    file_entries: list[tuple[str, bytes]] = []
    for rel, _ in content_entries:
        file_entries.append((rel, (src_dir / rel).read_bytes()))
    file_entries.append((f"{SKILL_META_DIR}/{MANIFEST_FILENAME}", manifest_json))

    src_meta = src_dir / SKILL_META_DIR
    if src_meta.is_dir():
        for meta_file in sorted(src_meta.rglob("*")):
            if not meta_file.is_file():
                continue
            rel = meta_file.relative_to(src_dir).as_posix()
            if rel == f"{SKILL_META_DIR}/{MANIFEST_FILENAME}":
                continue
            file_entries.append((rel, meta_file.read_bytes()))

    file_entries.sort(key=lambda e: e[0])
    dir_names = _unique_dirs([e[0] for e in file_entries])

    dest = output_dir or src_dir.parent
    dest.mkdir(parents=True, exist_ok=True)
    bundle_path = dest / f"{skill_name}-v{version}{BUNDLE_SUFFIX}"

    with open(bundle_path, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:  # type: ignore[arg-type]
                for d in dir_names:
                    tar.addfile(_make_tarinfo(d, is_dir=True))
                for arcname, data in file_entries:
                    tar.addfile(_make_tarinfo(arcname, size=len(data)), io.BytesIO(data))

    return bundle_path


def unpack_skill_bundle(bundle_path: Path, dest_dir: Path) -> BundleManifest:
    bundle_path = Path(bundle_path)
    dest_dir = Path(dest_dir)

    if not bundle_path.is_file():
        raise BundleStructureError(f"bundle file does not exist: {bundle_path}")

    try:
        with tarfile.open(bundle_path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in member.name.split("/"):
                    raise BundleStructureError(f"unsafe path in bundle: {member.name}")
            tar.extractall(dest_dir, filter="data")
    except BundleStructureError:
        raise
    except (tarfile.TarError, gzip.BadGzipFile, EOFError) as exc:
        raise BundleStructureError(f"failed to extract bundle: {exc}") from exc

    manifest_path = dest_dir / SKILL_META_DIR / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise BundleStructureError(
            f"bundle missing {SKILL_META_DIR}/{MANIFEST_FILENAME}"
        )

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BundleStructureError(f"invalid manifest JSON: {exc}") from exc

    return BundleManifest.from_dict(raw)


def verify_bundle_contents(manifest: BundleManifest, extracted_dir: Path) -> None:
    extracted_dir = Path(extracted_dir)

    for entry in manifest.files:
        file_path = extracted_dir / entry.path
        if not file_path.is_file():
            raise BundleContentMismatchError(
                f"file listed in manifest is missing: {entry.path}"
            )
        actual = _file_sha256(file_path)
        if actual != entry.sha256:
            raise BundleContentMismatchError(
                f"SHA256 mismatch for {entry.path}: "
                f"expected {entry.sha256}, got {actual}"
            )

    actual_tree = compute_tree_sha256(extracted_dir)
    if actual_tree != manifest.tree_sha256:
        raise BundleContentMismatchError(
            f"tree SHA256 mismatch: expected {manifest.tree_sha256}, got {actual_tree}"
        )
