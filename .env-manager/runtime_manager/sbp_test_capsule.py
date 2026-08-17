"""source-capsule/v1: what exactly was tested (skillbox-sbp-test-source-capsule-e1jj).

`sbp test` runs a repo's tests on other compute, so a receipt is worthless
unless it can say *which bytes ran*. A capsule captures the working tree --
including uncommitted work -- and identifies it three separate ways.

Three non-interchangeable identifiers
-------------------------------------
``source_tree_oid``
    A real Git tree object built through a temporary ``GIT_INDEX_FILE``
    (``read-tree HEAD`` -> ``add -A`` -> ``write-tree``). It is the identity Git
    itself would give these contents. It is **not** durable evidence on its own:
    an unreferenced tree is GC-prunable.
``capsule_manifest_sha256``
    Digest of the materialized inventory -- bytes, modes, symlink targets and
    policy. Recomputable *after extraction*, which is what lets a worker prove
    it received what the host sent. Git's tree OID cannot do this: it normalizes
    away much of what actually gets written to disk.
``archive_sha256``
    The transport identity of the archive file. Detects corruption in flight and
    is the store's content address.

They answer different questions and are never substituted for one another.

Secrets: `.gitignore` is not a firewall
---------------------------------------
An ignore rule is a convenience, not a security boundary -- it is edited by
anyone, and an untracked secret sitting next to a missing rule is exactly the
file you least want shipped to another machine. Paths whose names look like
secrets are **refused, not warned about**, via ``scripts/lib/redaction``'s
``is_secret_key``. Refusal is the whole point: a warning in a log an agent does
not read is indistinguishable from silence.

Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

try:  # package import
    from .shared import is_secret_key  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - direct/script import fallback
    import sys

    _LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
    if str(_LIB) not in sys.path:
        sys.path.insert(0, str(_LIB))
    from redaction import is_secret_key  # type: ignore[no-redef]


CAPSULE_SCHEMA = "source-capsule/v1"
CAPSULE_STORE_RELPATH = ".skillbox-state/test-capsules"

STORE_DIR_MODE = 0o700
STORE_FILE_MODE = 0o600

#: Default store ceiling. A capsule store that grows without bound is a disk
#: outage waiting to happen on the box that can least afford one.
DEFAULT_QUOTA_BYTES = 2 * 1024 * 1024 * 1024

MANIFEST_HEADER = "capsule-manifest/v1"

KIND_FILE = "file"
KIND_SYMLINK = "symlink"


class CapsuleRefusal(Exception):
    """A typed, fail-closed refusal to build or admit a capsule."""

    def __init__(self, code: str, message: str, *, paths: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.paths = sorted(paths)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "error_code": self.code, "error": self.message}
        if self.paths:
            payload["paths"] = list(self.paths)
        return payload


@dataclass(frozen=True)
class CapsuleEntry:
    """One materialized path in the capsule."""

    path: str
    kind: str
    mode: int
    size: int
    digest: str  # sha256 of contents, or of the symlink target bytes

    def to_row(self) -> str:
        # JSON per line: a path may contain newlines, quotes or unicode, and a
        # naive space-separated format would be ambiguous exactly where an
        # attacker would aim.
        return json.dumps(
            [self.path, self.kind, self.mode, self.size, self.digest],
            ensure_ascii=True,
            sort_keys=False,
        )


@dataclass(frozen=True)
class Inventory:
    """Plan-visible summary of what the capsule captured."""

    modified: int = 0
    deleted: int = 0
    untracked: int = 0
    exclusions: int = 0

    def to_payload(self) -> dict[str, int]:
        return {
            "modified": self.modified,
            "deleted": self.deleted,
            "untracked": self.untracked,
            "exclusions": self.exclusions,
        }


@dataclass(frozen=True)
class Capsule:
    schema: str
    source_tree_oid: str
    capsule_manifest_sha256: str
    archive_sha256: str
    entry_count: int
    total_bytes: int
    inventory: Inventory
    archive_path: Path | None = None
    entries: tuple[CapsuleEntry, ...] = field(default=(), repr=False)

    def identifiers(self) -> dict[str, str]:
        """The three identifiers, always stamped together."""
        return {
            "source_tree_oid": self.source_tree_oid,
            "capsule_manifest_sha256": self.capsule_manifest_sha256,
            "archive_sha256": self.archive_sha256,
        }

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            **self.identifiers(),
            "entry_count": self.entry_count,
            "total_bytes": self.total_bytes,
            "inventory": self.inventory.to_payload(),
        }
        if self.archive_path is not None:
            payload["archive_path"] = str(self.archive_path)
        return payload


# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    full_env = dict(os.environ)
    # Keep the caller's hooks/config from steering plumbing we depend on.
    full_env.setdefault("GIT_CONFIG_NOSYSTEM", "1")
    if env:
        full_env.update(env)
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=full_env,
    )
    if result.returncode != 0:
        raise CapsuleRefusal(
            "git_failed",
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}",
        )
    return result.stdout


def build_source_tree_oid(repo: Path, *, ephemeral_objects: bool = False) -> str:
    """Write a Git tree for the CURRENT working tree without touching the index.

    A temporary ``GIT_INDEX_FILE`` is essential: doing this against the real
    index would stage the operator's working tree as a side effect of asking a
    read-only question.

    ``ephemeral_objects`` additionally redirects *object writes* to a throwaway
    directory. ``git write-tree`` materializes new blob/tree objects, and with
    uncommitted work those land in the caller's ``.git/objects`` -- which is a
    real (if GC-able) side effect. Plan mode must not do that, so it asks for
    ephemeral objects; the capsule path keeps the default because persisting the
    objects there is the point.
    """
    repo = Path(repo)
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "capsule.index")}
        if ephemeral_objects:
            scratch = Path(tmp) / "objects"
            scratch.mkdir()
            real_objects = _git(repo, "rev-parse", "--git-path", "objects").strip()
            real_objects = str((repo / real_objects).resolve())
            # New objects land in `scratch`; existing ones stay readable through
            # the alternate, so read-tree/add still resolve HEAD normally.
            env["GIT_OBJECT_DIRECTORY"] = str(scratch)
            env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = real_objects
        _git(repo, "read-tree", "HEAD", env=env)
        _git(repo, "add", "-A", env=env)
        return _git(repo, "write-tree", env=env).strip()


def _porcelain_entries(repo: Path) -> list[tuple[str, str]]:
    """(status_xy, path) from `git status --porcelain=v1 -z`.

    NUL-delimited so filenames containing newlines, quotes or unicode survive;
    the default output would quote-escape them and re-parsing that is where
    filename bugs live.
    """
    raw = _git(repo, "status", "--porcelain=v1", "-z", "--no-renames")
    out: list[tuple[str, str]] = []
    for chunk in raw.split("\0"):
        if len(chunk) < 4:
            continue
        out.append((chunk[:2], chunk[3:]))
    return out


def collect_inventory(repo: Path, exclusions: int = 0) -> Inventory:
    modified = deleted = untracked = 0
    for status, _path in _porcelain_entries(repo):
        if status == "??":
            untracked += 1
        elif "D" in status:
            deleted += 1
        else:
            modified += 1
    return Inventory(
        modified=modified, deleted=deleted, untracked=untracked, exclusions=exclusions
    )


def dirty_submodules(repo: Path) -> list[str]:
    """Submodules that are not at a clean, recorded commit.

    v1 refuses rather than guessing: a capsule that silently captured a
    submodule gitlink while the submodule's own working tree differed would
    claim to identify contents it never saw.
    """
    raw = _git(repo, "submodule", "status", "--recursive")
    dirty: set[str] = set()
    known: set[str] = set()
    for line in raw.splitlines():
        if not line.strip():
            continue
        marker = line[0]
        parts = line[1:].split()
        if len(parts) < 2:
            continue
        path = parts[1]
        known.add(path)
        # `+` recorded commit differs, `-` uninitialized, `U` conflicted.
        if marker in "+-U":
            dirty.add(path)

    if known:
        # `git submodule status` reports a CLEAN space marker when only the
        # submodule's working tree is dirty -- its marker tracks the recorded
        # COMMIT, not the content. Modified content shows up instead as ` M sub`
        # in the superproject's porcelain, so both sources are required or a
        # dirty submodule sails straight through.
        for status, path in _porcelain_entries(repo):
            candidate = path.rstrip("/")
            if candidate in known and status.strip():
                dirty.add(candidate)
    return sorted(dirty)


# --------------------------------------------------------------------------- #
# policy screening
# --------------------------------------------------------------------------- #


def secret_shaped_paths(paths: Iterable[str]) -> list[str]:
    """Paths whose NAME signals a secret, using the shared redaction table.

    Screens every component, not just the basename: `secrets/prod/value.txt`
    is as much a refusal as `prod.token`.
    """
    hits: list[str] = []
    for path in paths:
        parts = PurePosixPath(path).parts
        if any(is_secret_key(part) for part in parts):
            hits.append(path)
    return sorted(hits)


def _escaping_symlinks(repo: Path, entries: Iterable[str]) -> list[str]:
    """Symlinks whose target resolves outside the repository."""
    repo = Path(repo).resolve()
    escaping: list[str] = []
    for rel in entries:
        full = repo / rel
        if not full.is_symlink():
            continue
        target = os.readlink(full)
        resolved = (full.parent / target).resolve() if not os.path.isabs(target) else Path(target).resolve()
        try:
            resolved.relative_to(repo)
        except ValueError:
            escaping.append(rel)
    return sorted(escaping)


def _capsule_paths(repo: Path) -> list[str]:
    """Every path the capsule would materialize: tracked + non-ignored untracked.

    Deliberately excludes ignored paths. `.gitignore` is not trusted as a
    *security* boundary (secret screening below is), but it is a legitimate
    "this is build output" signal, and v1 has no way to force one in.
    """
    tracked = [p for p in _git(repo, "ls-files", "-z").split("\0") if p]
    untracked = [
        p
        for p in _git(
            repo, "ls-files", "-z", "--others", "--exclude-standard"
        ).split("\0")
        if p
    ]
    return sorted(set(tracked) | set(untracked))


def screen_paths(repo: Path, paths: Iterable[str], *, allow_ignored: Iterable[str] = ()) -> None:
    """Fail closed on everything v1 refuses. Raises :class:`CapsuleRefusal`."""
    allow_ignored = list(allow_ignored)
    if allow_ignored:
        raise CapsuleRefusal(
            "ignored_path_allowlist_refused",
            "v1 refuses an ignored-path allowlist: re-including an ignored path "
            "is how build output and local secrets reach another machine",
            paths=allow_ignored,
        )

    paths = list(paths)
    secrets = secret_shaped_paths(paths)
    if secrets:
        raise CapsuleRefusal(
            "secret_shaped_path",
            "refusing to capsule secret-shaped paths; .gitignore is not a "
            "firewall, so these are refused rather than warned about",
            paths=secrets,
        )

    escaping = _escaping_symlinks(repo, paths)
    if escaping:
        raise CapsuleRefusal(
            "symlink_escape",
            "refusing symlinks whose target resolves outside the repository",
            paths=escaping,
        )

    dirty = dirty_submodules(repo)
    if dirty:
        raise CapsuleRefusal(
            "dirty_submodule",
            "refusing to capsule a tree with dirty submodules; the capsule "
            "could not honestly identify their contents",
            paths=dirty,
        )


# --------------------------------------------------------------------------- #
# materialization
# --------------------------------------------------------------------------- #


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def build_entries(repo: Path, paths: Iterable[str]) -> list[CapsuleEntry]:
    repo = Path(repo)
    entries: list[CapsuleEntry] = []
    for rel in sorted(paths):
        full = repo / rel
        if full.is_symlink():
            target = os.readlink(full).encode("utf-8", "surrogateescape")
            entries.append(
                CapsuleEntry(rel, KIND_SYMLINK, 0o120777, len(target), _sha256_bytes(target))
            )
            continue
        if not full.is_file():
            # Deleted between listing and materialization, or a directory entry.
            continue
        digest, size = _sha256_file(full)
        mode = 0o100755 if os.access(full, os.X_OK) else 0o100644
        entries.append(CapsuleEntry(rel, KIND_FILE, mode, size, digest))
    return entries


def manifest_text(entries: Iterable[CapsuleEntry]) -> str:
    rows = [MANIFEST_HEADER]
    rows.extend(entry.to_row() for entry in sorted(entries, key=lambda e: e.path))
    return "\n".join(rows) + "\n"


def compute_manifest_sha256(entries: Iterable[CapsuleEntry]) -> str:
    return _sha256_bytes(manifest_text(entries).encode("utf-8"))


def write_archive(repo: Path, entries: Iterable[CapsuleEntry], dest: Path) -> str:
    """Write a deterministic tar of the entries and return its sha256.

    Deterministic on purpose: identical contents must produce an identical
    archive digest on any host, or the transport identity carries no meaning.
    """
    repo = Path(repo)
    entries = sorted(entries, key=lambda e: e.path)
    with tarfile.open(dest, "w", format=tarfile.PAX_FORMAT) as tar:
        for entry in entries:
            info = tarfile.TarInfo(name=entry.path)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            if entry.kind == KIND_SYMLINK:
                info.type = tarfile.SYMTYPE
                info.linkname = os.readlink(repo / entry.path)
                info.mode = 0o777
                tar.addfile(info)
                continue
            info.type = tarfile.REGTYPE
            info.size = entry.size
            info.mode = 0o755 if entry.mode == 0o100755 else 0o644
            with open(repo / entry.path, "rb") as handle:
                tar.addfile(info, handle)
    digest, _size = _sha256_file(dest)
    return digest


# --------------------------------------------------------------------------- #
# content-addressed store
# --------------------------------------------------------------------------- #


def store_root(repo: Path, override: str | os.PathLike[str] | None = None) -> Path:
    if override is not None:
        return Path(override)
    env = os.environ.get("SKILLBOX_TEST_CAPSULE_STORE")
    if env:
        return Path(env)
    return Path(repo) / CAPSULE_STORE_RELPATH


def ensure_store(root: Path) -> Path:
    root = Path(root)
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    # 0700: a capsule is a verbatim copy of a working tree. Even after secret
    # screening it is the operator's source, and it is not world-readable.
    os.chmod(root, STORE_DIR_MODE)
    os.chmod(root / "tmp", STORE_DIR_MODE)
    return root


def store_usage_bytes(root: Path) -> int:
    root = Path(root)
    if not root.is_dir():
        return 0
    total = 0
    for path in root.glob("*.tar"):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def prune_store_temp(root: Path) -> int:
    """Remove abandoned staging files (an interrupted build leaves them)."""
    tmp = Path(root) / "tmp"
    removed = 0
    if not tmp.is_dir():
        return 0
    for path in tmp.iterdir():
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def admit(
    staged: Path,
    root: Path,
    archive_sha256: str,
    *,
    quota_bytes: int = DEFAULT_QUOTA_BYTES,
) -> Path:
    """Atomically admit a verified archive into the content-addressed store.

    Order matters and is the whole contract: **verify, then admit**. The staged
    file is re-hashed from disk (not trusted from the builder), then linked into
    place with ``O_EXCL`` semantics so a concurrent admission of the same
    content cannot interleave into a half-written file.
    """
    staged = Path(staged)
    root = ensure_store(Path(root))

    actual, size = _sha256_file(staged)
    if actual != archive_sha256:
        staged.unlink(missing_ok=True)
        raise CapsuleRefusal(
            "archive_digest_mismatch",
            f"staged archive digest {actual} does not match expected {archive_sha256}; "
            "refusing to admit a corrupt capsule",
        )

    final = root / f"{archive_sha256}.tar"
    if final.exists():
        # Duplicate admission is idempotent, not an error: the store is content
        # addressed, so identical content is already the same object.
        staged.unlink(missing_ok=True)
        return final

    usage = store_usage_bytes(root)
    if quota_bytes >= 0 and usage + size > quota_bytes:
        staged.unlink(missing_ok=True)
        raise CapsuleRefusal(
            "capsule_store_quota_exceeded",
            f"capsule store quota exceeded: {usage + size} > {quota_bytes} bytes",
        )

    try:
        os.link(staged, final)
    except FileExistsError:
        # Lost a race with a concurrent admit of the same content; that is fine.
        staged.unlink(missing_ok=True)
        return final
    os.chmod(final, STORE_FILE_MODE)
    staged.unlink(missing_ok=True)
    return final


def verify_stored(root: Path, archive_sha256: str) -> bool:
    """Re-hash a stored archive. Catches at-rest corruption."""
    path = Path(root) / f"{archive_sha256}.tar"
    if not path.is_file():
        return False
    actual, _size = _sha256_file(path)
    return actual == archive_sha256


def manifest_from_archive(archive: Path) -> list[CapsuleEntry]:
    """Recompute the manifest from an extracted archive.

    This is what makes ``capsule_manifest_sha256`` meaningful: a worker can
    derive the same digest from what it actually received.
    """
    entries: list[CapsuleEntry] = []
    with tarfile.open(archive, "r") as tar:
        for info in tar.getmembers():
            if info.issym():
                target = info.linkname.encode("utf-8", "surrogateescape")
                entries.append(
                    CapsuleEntry(info.name, KIND_SYMLINK, 0o120777, len(target), _sha256_bytes(target))
                )
                continue
            if not info.isfile():
                continue
            handle = tar.extractfile(info)
            data = handle.read() if handle else b""
            mode = 0o100755 if info.mode & 0o111 else 0o100644
            entries.append(
                CapsuleEntry(info.name, KIND_FILE, mode, len(data), _sha256_bytes(data))
            )
    return entries


# --------------------------------------------------------------------------- #
# top level
# --------------------------------------------------------------------------- #


def compute_digests(repo: Path) -> dict[str, str]:
    """The three identifiers, computed with **no durable side effects**.

    The plan compiler needs to name the source it is planning against, but plan
    mode must not write: it neither admits to the capsule store nor creates it.
    The archive is built inside a throwaway temp dir purely to derive the
    transport digest, and is deleted with it. Because the archive format is
    deterministic (no mtime/uid/gid/uname), the digest is identical to the one a
    later real admission would produce. Git objects are ephemeral too, so this
    leaves the caller's object database untouched.
    """
    repo = Path(repo)
    paths = _capsule_paths(repo)
    screen_paths(repo, paths)
    entries = build_entries(repo, paths)
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "capsule.tar"
        archive_sha = write_archive(repo, entries, staged)
    return {
        "source_tree_oid": build_source_tree_oid(repo, ephemeral_objects=True),
        "capsule_manifest_sha256": compute_manifest_sha256(entries),
        "archive_sha256": archive_sha,
    }


def build_capsule(
    repo: Path,
    *,
    store: str | os.PathLike[str] | None = None,
    quota_bytes: int = DEFAULT_QUOTA_BYTES,
    admit_to_store: bool = True,
    allow_ignored: Iterable[str] = (),
) -> Capsule:
    """Build, verify and (by default) admit a source capsule.

    Raises :class:`CapsuleRefusal` on any v1 refusal. Never partially admits.
    """
    repo = Path(repo)
    paths = _capsule_paths(repo)
    screen_paths(repo, paths, allow_ignored=allow_ignored)

    source_tree_oid = build_source_tree_oid(repo)
    entries = build_entries(repo, paths)
    manifest_sha = compute_manifest_sha256(entries)
    inventory = collect_inventory(repo)
    total_bytes = sum(entry.size for entry in entries)

    root = ensure_store(store_root(repo, store))
    staged_fd, staged_name = tempfile.mkstemp(dir=root / "tmp", suffix=".tar.part")
    os.close(staged_fd)
    staged = Path(staged_name)
    try:
        archive_sha = write_archive(repo, entries, staged)
        archive_path: Path | None = None
        if admit_to_store:
            archive_path = admit(staged, root, archive_sha, quota_bytes=quota_bytes)
        else:
            staged.unlink(missing_ok=True)
    except CapsuleRefusal:
        staged.unlink(missing_ok=True)
        raise
    except Exception:
        staged.unlink(missing_ok=True)
        raise

    return Capsule(
        schema=CAPSULE_SCHEMA,
        source_tree_oid=source_tree_oid,
        capsule_manifest_sha256=manifest_sha,
        archive_sha256=archive_sha,
        entry_count=len(entries),
        total_bytes=total_bytes,
        inventory=inventory,
        archive_path=archive_path,
        entries=tuple(entries),
    )
