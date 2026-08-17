"""``family/v1`` manifest and ``family-snapshot/v1`` receipt schemas.

A *family* is the set of co-deployed repos, services, data mounts, and external
enrollments that move together — the layer above ``BoxProfile``, which carries
exactly one repo. This module defines what a family declares and what a snapshot
of one attests. It defines **only** those two contracts: snapshot, pause, and
resume execution belong to sibling beads, and nothing here starts, stops,
snapshots, or destroys anything.

The epic's four-layer model is what makes the receipt small:

* **L0** OS baseline — the golden image, shared across families, never captured.
* **L1** code — commit SHAs plus content-addressed capsules, never byte-copied.
* **L2** data — the volume snapshot, and the only thing actually snapshotted.
* **L3** identity — node keys, ``machine_id``, auth tokens — **never** captured,
  because a restored machine must come up with a fresh identity rather than
  impersonate its ancestor.

So the receipt *is* the snapshot; the volume snapshot is merely its largest
attachment. Everything else is a digest or a SHA.

Two disciplines are enforced rather than documented:

**Never lie.** A receipt may not claim evidence it does not carry. A member
marked dirty must carry a capsule digest — otherwise the receipt asserts a
working tree was captured when nothing was. A clean member must *not* carry one,
because that describes work that did not happen.
:func:`verify_receipt_covers_manifest` extends the same rule across documents: a
receipt that omits a data mount the family declared is claiming a family
snapshot it does not have.

**L3 is structurally impossible, not merely absent.** Both documents run an
exact key allowlist *and* a recursive scan that refuses secret-shaped keys and
identity material anywhere at any depth, using the same
``scripts/lib/redaction`` table (``is_secret_key``) that
:mod:`runtime_manager.sbp_test_capsule` screens capsule paths with. One
implementation of "what looks like a secret", shared by both surfaces. Refusal,
never a warning: a warning in a log an agent does not read is not a control.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

try:  # PyYAML is optional across this codebase; YAML entry points guard on it.
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised by the guard test
    yaml = None  # type: ignore[assignment]

from .sbp_test_capsule import secret_shaped_paths

FAMILY_MANIFEST_SCHEMA = "family/v1"
FAMILY_SNAPSHOT_SCHEMA = "family-snapshot/v1"

MANIFEST_KEYS = frozenset(
    {"schema", "name", "members", "services", "data_mounts", "enrollments"}
)
MEMBER_KEYS = frozenset({"repo", "path", "branch", "commit"})
SERVICE_KEYS = frozenset({"id", "kind", "quiesce", "version"})
DATA_MOUNT_KEYS = frozenset({"id", "path"})
ENROLLMENT_KEYS = frozenset({"id", "kind", "revoke_on_pause"})

RECEIPT_KEYS = frozenset(
    {
        "schema",
        "family",
        "snapshot_id",
        "created_at",
        "members",
        "services",
        "volume_snapshots",
        "enrollments_revoked",
        "resumed_from",
    }
)
RECEIPT_MEMBER_KEYS = frozenset({"repo", "commit", "dirty", "capsule_digest"})
RECEIPT_SERVICE_KEYS = frozenset({"id", "version"})
VOLUME_SNAPSHOT_KEYS = frozenset({"mount_id", "snapshot_id", "size_bytes"})

#: How a service is brought to rest before its data mount is snapshotted. A
#: snapshot taken under an active writer is a torn one, so "none" must be an
#: explicit declaration rather than an omission.
QUIESCE_MODES = ("drain", "stop", "flush", "none")

#: External identity a family holds. Every kind here is revocable on pause and
#: re-issued on resume; none of it is ever snapshotted.
ENROLLMENT_KINDS = ("tailscale", "dns", "webhook", "api", "registry")

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
REPO_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$")
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SNAPSHOT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
VOLUME_SNAPSHOT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)

MAX_MEMBERS = 32
MAX_SERVICES = 32
MAX_DATA_MOUNTS = 16
MAX_ENROLLMENTS = 16
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_DEPTH = 8
MAX_VOLUME_BYTES = 16 * 1024**4

#: L3 material. Named explicitly on top of the shared secret table because
#: `machine_id` and `node_key` do not match a TOKEN/SECRET/PASSWORD heuristic,
#: yet re-using either is exactly the ancestor impersonation the epic forbids.
IDENTITY_KEY_NAMES = frozenset(
    {
        "machineid",
        "machine",
        "nodekey",
        "node",
        "nodeid",
        "tailscalekey",
        "authkey",
        "identity",
        "hostkey",
        "sshkey",
        "tlskey",
        "certificate",
        "cert",
        "privatekey",
        "session",
        "cookie",
    }
)

REFUSAL_CODES = frozenset(
    {
        "document_invalid",
        "document_too_large",
        "document_too_deep",
        "identity_material_forbidden",
        "manifest_invalid",
        "receipt_incomplete",
        "receipt_invalid",
        "secret_shaped_path",
        "yaml_unavailable",
    }
)


class FamilySchemaError(Exception):
    """A typed, fail-closed refusal. Never carries the offending value."""

    def __init__(self, code: str, message: str, *, paths: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.paths = sorted(paths)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error_code": self.code,
            "error": self.message,
        }
        if self.paths:
            payload["paths"] = list(self.paths)
        return payload


def _refuse(code: str, message: str, *, paths: Iterable[str] = ()) -> Any:
    raise FamilySchemaError(code, message, paths=paths)


def _normalize_key(key: str) -> str:
    return key.replace("_", "").replace("-", "").strip().lower()


def assert_no_identity_material(document: Any, _depth: int = 0) -> None:
    """Refuse any key that names L3 material, at any depth.

    Belt and braces over the exact key allowlists: an allowlist protects the
    shapes it knows about, and this protects the ones a future field might
    introduce. ``machine_id`` and ``node_key`` are listed by name because they
    carry no TOKEN/SECRET-shaped substring, yet reusing either is precisely the
    ancestor impersonation the family model forbids.
    """

    if _depth > MAX_DEPTH:
        _refuse("document_too_deep", "family document nests too deeply")
    if isinstance(document, Mapping):
        for key, value in document.items():
            if not isinstance(key, str):
                _refuse("document_invalid", "family document keys must be strings")
            normalized = _normalize_key(key)
            if normalized in IDENTITY_KEY_NAMES:
                _refuse(
                    "identity_material_forbidden",
                    f"L3 identity material is never captured: refusing key {key!r}",
                )
            from .sbp_test_capsule import is_secret_key  # local: shared table

            if is_secret_key(key):
                _refuse(
                    "identity_material_forbidden",
                    f"secret-shaped key {key!r} may not appear in a family document",
                )
            assert_no_identity_material(value, _depth + 1)
        return
    if isinstance(document, (list, tuple)):
        for item in document:
            assert_no_identity_material(item, _depth + 1)


def _screen_path(value: str, field: str) -> str:
    """A declared path must not name a secret in ANY component."""

    hits = secret_shaped_paths([value])
    if hits:
        _refuse(
            "secret_shaped_path",
            f"{field} names a secret-shaped path component",
            paths=hits,
        )
    parts = PurePosixPath(value).parts
    if ".." in parts:
        _refuse("document_invalid", f"{field} must not traverse with '..'")
    return value


def _exact(value: Any, keys: frozenset[str], code: str, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _refuse(code, f"{what} must be a mapping")
    present = set(value)
    unknown = sorted(present - keys)
    if unknown:
        _refuse(code, f"{what} carries unknown field(s): {', '.join(unknown)}")
    missing = sorted(set(keys) - present)
    if missing:
        _refuse(code, f"{what} is missing field(s): {', '.join(missing)}")
    return value


def _pattern(value: Any, pattern: re.Pattern[str], code: str, what: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _refuse(code, f"{what} is malformed")
    return value


def _bounded_list(value: Any, maximum: int, code: str, what: str) -> list[Any]:
    if not isinstance(value, list):
        _refuse(code, f"{what} must be a list")
    if len(value) > maximum:
        _refuse(code, f"{what} exceeds {maximum} entries")
    return value


def _flag(value: Any, code: str, what: str) -> bool:
    if type(value) is not bool:
        _refuse(code, f"{what} must be a boolean")
    return value


# --------------------------------------------------------------------------- #
# family/v1 manifest
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FamilyMember:
    """One repo in the family, and where its code is pinned."""

    repo: str
    path: str
    branch: str
    commit: str | None


@dataclass(frozen=True)
class FamilyService:
    """A writer that must come to rest before its data mount is snapshotted."""

    id: str
    kind: str
    quiesce: str
    version: str | None


@dataclass(frozen=True)
class FamilyDataMount:
    """L2: the only layer that is actually snapshotted."""

    id: str
    path: str


@dataclass(frozen=True)
class FamilyEnrollment:
    """External identity: revoked on pause, re-issued fresh on resume."""

    id: str
    kind: str
    revoke_on_pause: bool


@dataclass(frozen=True)
class FamilyManifest:
    """A validated ``family/v1`` declaration."""

    name: str
    members: tuple[FamilyMember, ...]
    services: tuple[FamilyService, ...]
    data_mounts: tuple[FamilyDataMount, ...]
    enrollments: tuple[FamilyEnrollment, ...]

    @property
    def mount_ids(self) -> tuple[str, ...]:
        return tuple(mount.id for mount in self.data_mounts)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": FAMILY_MANIFEST_SCHEMA,
            "name": self.name,
            "members": [
                {
                    "repo": member.repo,
                    "path": member.path,
                    "branch": member.branch,
                    "commit": member.commit,
                }
                for member in self.members
            ],
            "services": [
                {
                    "id": service.id,
                    "kind": service.kind,
                    "quiesce": service.quiesce,
                    "version": service.version,
                }
                for service in self.services
            ],
            "data_mounts": [
                {"id": mount.id, "path": mount.path} for mount in self.data_mounts
            ],
            "enrollments": [
                {
                    "id": enrollment.id,
                    "kind": enrollment.kind,
                    "revoke_on_pause": enrollment.revoke_on_pause,
                }
                for enrollment in self.enrollments
            ],
        }

    @classmethod
    def from_mapping(cls, document: Any) -> FamilyManifest:
        _assert_document_size(document)
        assert_no_identity_material(document)
        raw = _exact(document, MANIFEST_KEYS, "manifest_invalid", "family manifest")
        if raw["schema"] != FAMILY_MANIFEST_SCHEMA:
            _refuse(
                "manifest_invalid",
                f"family manifest schema must be {FAMILY_MANIFEST_SCHEMA}",
            )
        name = _pattern(raw["name"], NAME_PATTERN, "manifest_invalid", "family name")

        members = _bounded_list(
            raw["members"], MAX_MEMBERS, "manifest_invalid", "members"
        )
        if not members:
            # A family with no repos is not a family; it is a machine.
            _refuse("manifest_invalid", "a family must declare at least one member")
        parsed_members = tuple(_member(entry) for entry in members)
        _assert_unique(
            [member.repo for member in parsed_members], "manifest_invalid", "member repo"
        )

        services = tuple(
            _service(entry)
            for entry in _bounded_list(
                raw["services"], MAX_SERVICES, "manifest_invalid", "services"
            )
        )
        _assert_unique(
            [service.id for service in services], "manifest_invalid", "service id"
        )

        mounts = tuple(
            _data_mount(entry)
            for entry in _bounded_list(
                raw["data_mounts"], MAX_DATA_MOUNTS, "manifest_invalid", "data_mounts"
            )
        )
        _assert_unique([mount.id for mount in mounts], "manifest_invalid", "data mount id")

        enrollments = tuple(
            _enrollment(entry)
            for entry in _bounded_list(
                raw["enrollments"], MAX_ENROLLMENTS, "manifest_invalid", "enrollments"
            )
        )
        _assert_unique(
            [item.id for item in enrollments], "manifest_invalid", "enrollment id"
        )

        return cls(
            name=name,
            members=parsed_members,
            services=services,
            data_mounts=mounts,
            enrollments=enrollments,
        )


def _assert_unique(values: list[str], code: str, what: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            _refuse(code, f"duplicate {what}: {value}")
        seen.add(value)


def _member(entry: Any) -> FamilyMember:
    raw = _exact(entry, MEMBER_KEYS, "manifest_invalid", "family member")
    commit = raw["commit"]
    if commit is not None:
        commit = _pattern(commit, COMMIT_PATTERN, "manifest_invalid", "member commit")
    return FamilyMember(
        repo=_pattern(raw["repo"], REPO_PATTERN, "manifest_invalid", "member repo"),
        path=_screen_path(
            _pattern(raw["path"], re.compile(r"^\S.{0,255}$"), "manifest_invalid", "member path"),
            "member path",
        ),
        branch=_pattern(
            raw["branch"], BRANCH_PATTERN, "manifest_invalid", "member branch"
        ),
        commit=commit,
    )


def _service(entry: Any) -> FamilyService:
    raw = _exact(entry, SERVICE_KEYS, "manifest_invalid", "family service")
    quiesce = raw["quiesce"]
    if quiesce not in QUIESCE_MODES:
        # "none" must be declared, never implied: a snapshot taken under an
        # active writer is a torn one, and silence is not a decision.
        _refuse(
            "manifest_invalid",
            f"service quiesce must be one of {', '.join(QUIESCE_MODES)}",
        )
    version = raw["version"]
    if version is not None:
        version = _pattern(
            version, VERSION_PATTERN, "manifest_invalid", "service version"
        )
    return FamilyService(
        id=_pattern(raw["id"], ID_PATTERN, "manifest_invalid", "service id"),
        kind=_pattern(raw["kind"], ID_PATTERN, "manifest_invalid", "service kind"),
        quiesce=quiesce,
        version=version,
    )


def _data_mount(entry: Any) -> FamilyDataMount:
    raw = _exact(entry, DATA_MOUNT_KEYS, "manifest_invalid", "family data mount")
    path = _pattern(
        raw["path"], re.compile(r"^/\S{0,255}$"), "manifest_invalid", "data mount path"
    )
    return FamilyDataMount(
        id=_pattern(raw["id"], ID_PATTERN, "manifest_invalid", "data mount id"),
        path=_screen_path(path, "data mount path"),
    )


def _enrollment(entry: Any) -> FamilyEnrollment:
    raw = _exact(entry, ENROLLMENT_KEYS, "manifest_invalid", "family enrollment")
    if raw["kind"] not in ENROLLMENT_KINDS:
        _refuse(
            "manifest_invalid",
            f"enrollment kind must be one of {', '.join(ENROLLMENT_KINDS)}",
        )
    return FamilyEnrollment(
        id=_pattern(raw["id"], ID_PATTERN, "manifest_invalid", "enrollment id"),
        kind=raw["kind"],
        revoke_on_pause=_flag(
            raw["revoke_on_pause"], "manifest_invalid", "enrollment revoke_on_pause"
        ),
    )


def _assert_document_size(document: Any) -> None:
    try:
        encoded = json.dumps(document, default=str).encode("utf-8")
    except (TypeError, ValueError):
        _refuse("document_invalid", "family document is not JSON-representable")
    if len(encoded) > MAX_DOCUMENT_BYTES:
        _refuse("document_too_large", "family document exceeds the size budget")


def load_manifest_text(text: str) -> FamilyManifest:
    """Parse a ``family.yaml`` document. Requires PyYAML, and says so if absent."""

    if yaml is None:
        _refuse(
            "yaml_unavailable",
            "PyYAML is required to read a family manifest; install it or pass a mapping",
        )
    if not isinstance(text, str):
        _refuse("manifest_invalid", "family manifest text must be a string")
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        _refuse("document_too_large", "family manifest exceeds the size budget")
    try:
        document = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - any parse failure is one refusal
        _refuse("manifest_invalid", "family manifest is not valid YAML")
    return FamilyManifest.from_mapping(document)


def load_manifest(path: str | Path) -> FamilyManifest:
    """Read and validate a family manifest from disk. Read-only."""

    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        _refuse("manifest_invalid", f"family manifest is unreadable: {target.name}")
    return load_manifest_text(text)


# --------------------------------------------------------------------------- #
# family-snapshot/v1 receipt
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SnapshotMember:
    """L1: a pinned commit, plus a capsule digest when the tree was dirty."""

    repo: str
    commit: str
    dirty: bool
    capsule_digest: str | None

    def __post_init__(self) -> None:
        # The never-lie rule, enforced at construction so no code path can
        # assemble a receipt that claims evidence it does not carry.
        if self.dirty and not self.capsule_digest:
            _refuse(
                "receipt_incomplete",
                f"member {self.repo!r} is dirty but carries no capsule digest",
            )
        if not self.dirty and self.capsule_digest:
            _refuse(
                "receipt_invalid",
                f"member {self.repo!r} is clean but carries a capsule digest",
            )


@dataclass(frozen=True)
class SnapshotService:
    id: str
    version: str


@dataclass(frozen=True)
class VolumeSnapshot:
    """L2: the one thing that is genuinely snapshotted."""

    mount_id: str
    snapshot_id: str
    size_bytes: int


@dataclass(frozen=True)
class FamilySnapshotReceipt:
    """An immutable ``family-snapshot/v1`` attestation.

    The receipt IS the snapshot: commit SHAs and capsule digests reconstruct L1,
    the volume snapshot ids name L2, and L0 comes from the golden image. L3 is
    absent by construction, so a resume necessarily issues fresh identity.
    """

    family: str
    snapshot_id: str
    created_at: str
    members: tuple[SnapshotMember, ...]
    services: tuple[SnapshotService, ...]
    volume_snapshots: tuple[VolumeSnapshot, ...]
    enrollments_revoked: tuple[str, ...]
    resumed_from: str | None

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema": FAMILY_SNAPSHOT_SCHEMA,
            "family": self.family,
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "members": [
                {
                    "repo": member.repo,
                    "commit": member.commit,
                    "dirty": member.dirty,
                    "capsule_digest": member.capsule_digest,
                }
                for member in self.members
            ],
            "services": [
                {"id": service.id, "version": service.version}
                for service in self.services
            ],
            "volume_snapshots": [
                {
                    "mount_id": volume.mount_id,
                    "snapshot_id": volume.snapshot_id,
                    "size_bytes": volume.size_bytes,
                }
                for volume in self.volume_snapshots
            ],
            "enrollments_revoked": list(self.enrollments_revoked),
            "resumed_from": self.resumed_from,
        }
        assert_no_identity_material(payload)
        return payload

    def canonical_bytes(self) -> bytes:
        """Byte-stable rendering, so a receipt digest is reproducible."""

        return json.dumps(
            self.to_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_mapping(cls, document: Any) -> FamilySnapshotReceipt:
        _assert_document_size(document)
        assert_no_identity_material(document)
        raw = _exact(document, RECEIPT_KEYS, "receipt_invalid", "family snapshot receipt")
        if raw["schema"] != FAMILY_SNAPSHOT_SCHEMA:
            _refuse(
                "receipt_invalid",
                f"receipt schema must be {FAMILY_SNAPSHOT_SCHEMA}",
            )
        resumed_from = raw["resumed_from"]
        if resumed_from is not None:
            resumed_from = _pattern(
                resumed_from, SNAPSHOT_ID_PATTERN, "receipt_invalid", "resumed_from"
            )
        snapshot_id = _pattern(
            raw["snapshot_id"], SNAPSHOT_ID_PATTERN, "receipt_invalid", "snapshot_id"
        )
        if resumed_from is not None and resumed_from == snapshot_id:
            _refuse("receipt_invalid", "a receipt cannot be resumed from itself")

        members = tuple(
            _receipt_member(entry)
            for entry in _bounded_list(
                raw["members"], MAX_MEMBERS, "receipt_invalid", "receipt members"
            )
        )
        if not members:
            _refuse("receipt_incomplete", "a family snapshot must attest a member")
        _assert_unique([m.repo for m in members], "receipt_invalid", "receipt member")

        services = tuple(
            _receipt_service(entry)
            for entry in _bounded_list(
                raw["services"], MAX_SERVICES, "receipt_invalid", "receipt services"
            )
        )
        _assert_unique([s.id for s in services], "receipt_invalid", "receipt service")

        volumes = tuple(
            _volume_snapshot(entry)
            for entry in _bounded_list(
                raw["volume_snapshots"],
                MAX_DATA_MOUNTS,
                "receipt_invalid",
                "volume_snapshots",
            )
        )
        _assert_unique([v.mount_id for v in volumes], "receipt_invalid", "volume mount")

        revoked = tuple(
            _pattern(item, ID_PATTERN, "receipt_invalid", "revoked enrollment id")
            for item in _bounded_list(
                raw["enrollments_revoked"],
                MAX_ENROLLMENTS,
                "receipt_invalid",
                "enrollments_revoked",
            )
        )
        _assert_unique(list(revoked), "receipt_invalid", "revoked enrollment")

        return cls(
            family=_pattern(raw["family"], NAME_PATTERN, "receipt_invalid", "family"),
            snapshot_id=snapshot_id,
            created_at=_pattern(
                raw["created_at"], TIMESTAMP_PATTERN, "receipt_invalid", "created_at"
            ),
            members=members,
            services=services,
            volume_snapshots=volumes,
            enrollments_revoked=revoked,
            resumed_from=resumed_from,
        )


def _receipt_member(entry: Any) -> SnapshotMember:
    raw = _exact(entry, RECEIPT_MEMBER_KEYS, "receipt_invalid", "receipt member")
    digest = raw["capsule_digest"]
    if digest is not None:
        digest = _pattern(digest, DIGEST_PATTERN, "receipt_invalid", "capsule_digest")
    return SnapshotMember(
        repo=_pattern(raw["repo"], REPO_PATTERN, "receipt_invalid", "receipt member repo"),
        commit=_pattern(raw["commit"], COMMIT_PATTERN, "receipt_invalid", "member commit"),
        dirty=_flag(raw["dirty"], "receipt_invalid", "member dirty"),
        capsule_digest=digest,
    )


def _receipt_service(entry: Any) -> SnapshotService:
    raw = _exact(entry, RECEIPT_SERVICE_KEYS, "receipt_invalid", "receipt service")
    return SnapshotService(
        id=_pattern(raw["id"], ID_PATTERN, "receipt_invalid", "receipt service id"),
        version=_pattern(
            raw["version"], VERSION_PATTERN, "receipt_invalid", "receipt service version"
        ),
    )


def _volume_snapshot(entry: Any) -> VolumeSnapshot:
    raw = _exact(entry, VOLUME_SNAPSHOT_KEYS, "receipt_invalid", "volume snapshot")
    size = raw["size_bytes"]
    if type(size) is not int or not 0 <= size <= MAX_VOLUME_BYTES:
        _refuse("receipt_invalid", "volume snapshot size_bytes is out of range")
    return VolumeSnapshot(
        mount_id=_pattern(raw["mount_id"], ID_PATTERN, "receipt_invalid", "mount_id"),
        snapshot_id=_pattern(
            raw["snapshot_id"],
            VOLUME_SNAPSHOT_ID_PATTERN,
            "receipt_invalid",
            "volume snapshot_id",
        ),
        size_bytes=size,
    )


def verify_receipt_covers_manifest(
    manifest: FamilyManifest, receipt: FamilySnapshotReceipt
) -> None:
    """Refuse a receipt that attests less than the family declared.

    The never-lie rule across documents. A receipt missing a declared data mount
    is claiming a family snapshot it does not have — on resume that mount comes
    back empty, and the receipt gave no warning.
    """

    if manifest.name != receipt.family:
        _refuse(
            "receipt_invalid",
            f"receipt is for family {receipt.family!r}, not {manifest.name!r}",
        )
    declared_members = {member.repo for member in manifest.members}
    attested_members = {member.repo for member in receipt.members}
    missing_members = sorted(declared_members - attested_members)
    if missing_members:
        _refuse(
            "receipt_incomplete",
            f"receipt omits declared member(s): {', '.join(missing_members)}",
        )
    extra_members = sorted(attested_members - declared_members)
    if extra_members:
        _refuse(
            "receipt_invalid",
            f"receipt attests undeclared member(s): {', '.join(extra_members)}",
        )
    declared_mounts = set(manifest.mount_ids)
    attested_mounts = {volume.mount_id for volume in receipt.volume_snapshots}
    missing_mounts = sorted(declared_mounts - attested_mounts)
    if missing_mounts:
        _refuse(
            "receipt_incomplete",
            f"receipt omits declared data mount(s): {', '.join(missing_mounts)}",
        )
    extra_mounts = sorted(attested_mounts - declared_mounts)
    if extra_mounts:
        _refuse(
            "receipt_invalid",
            f"receipt attests undeclared data mount(s): {', '.join(extra_mounts)}",
        )
    must_revoke = {
        enrollment.id for enrollment in manifest.enrollments if enrollment.revoke_on_pause
    }
    revoked = set(receipt.enrollments_revoked)
    unrevoked = sorted(must_revoke - revoked)
    if unrevoked:
        # Pause means destroy; an enrollment left live outlives the family it
        # belonged to and lets a restored machine inherit reachability.
        _refuse(
            "receipt_incomplete",
            f"receipt omits revoke-on-pause enrollment(s): {', '.join(unrevoked)}",
        )


__all__ = [
    "DATA_MOUNT_KEYS",
    "ENROLLMENT_KEYS",
    "ENROLLMENT_KINDS",
    "FAMILY_MANIFEST_SCHEMA",
    "FAMILY_SNAPSHOT_SCHEMA",
    "IDENTITY_KEY_NAMES",
    "MANIFEST_KEYS",
    "MAX_DATA_MOUNTS",
    "MAX_MEMBERS",
    "MEMBER_KEYS",
    "QUIESCE_MODES",
    "RECEIPT_KEYS",
    "REFUSAL_CODES",
    "SERVICE_KEYS",
    "VOLUME_SNAPSHOT_KEYS",
    "FamilyDataMount",
    "FamilyEnrollment",
    "FamilyManifest",
    "FamilyMember",
    "FamilySchemaError",
    "FamilyService",
    "FamilySnapshotReceipt",
    "SnapshotMember",
    "SnapshotService",
    "VolumeSnapshot",
    "assert_no_identity_material",
    "load_manifest",
    "load_manifest_text",
    "verify_receipt_covers_manifest",
]
