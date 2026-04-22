"""Lockfile schema helpers for distribution-aware skill sync."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

LOCKFILE_VERSION = 3
DEFAULT_SKILL_SOURCE = "repo"


class LockfileSchemaError(RuntimeError):
    """Raised when lockfile payloads fail schema validation."""


@dataclass(frozen=True)
class DistributorManifestLockEntry:
    manifest_version: int
    fetched_at: str
    signature_verified: bool

    @classmethod
    def from_dict(
        cls,
        distributor_id: str,
        payload: Mapping[str, Any],
    ) -> "DistributorManifestLockEntry":
        if not isinstance(payload, Mapping):
            raise LockfileSchemaError(
                f"distributor_manifests[{distributor_id!r}] must be an object"
            )
        manifest_version = _coerce_int(
            payload.get("manifest_version"),
            f"distributor_manifests[{distributor_id!r}].manifest_version",
        )
        fetched_at = _coerce_str(
            payload.get("fetched_at"),
            f"distributor_manifests[{distributor_id!r}].fetched_at",
        )
        signature_verified = _coerce_bool(
            payload.get("signature_verified"),
            f"distributor_manifests[{distributor_id!r}].signature_verified",
        )
        return cls(
            manifest_version=manifest_version,
            fetched_at=fetched_at,
            signature_verified=signature_verified,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "fetched_at": self.fetched_at,
            "signature_verified": self.signature_verified,
        }


@dataclass(frozen=True)
class SkillLockEntry:
    name: str
    source: str = DEFAULT_SKILL_SOURCE
    repo: str | None = None
    source_path: str | None = None
    declared_ref: str | None = None
    resolved_commit: str | None = None
    install_tree_sha: str | None = None
    bundle_sha256: str | None = None
    bundle_tree_sha256: str | None = None
    targets: list[dict[str, Any]] = field(default_factory=list)

    distributor_id: str | None = None
    version: int | None = None
    pinned_by: str | None = None
    pin_reason: str | None = None

    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SkillLockEntry":
        if not isinstance(payload, Mapping):
            raise LockfileSchemaError("lockfile skill entries must be objects")
        name = _coerce_str(payload.get("name"), "skills[].name")
        source = _coerce_optional_str(payload.get("source"), "skills[].source") or DEFAULT_SKILL_SOURCE

        targets_raw = payload.get("targets")
        targets: list[dict[str, Any]] = []
        if targets_raw is not None:
            if not isinstance(targets_raw, list):
                raise LockfileSchemaError(f"skill {name!r} targets must be a list")
            for index, target in enumerate(targets_raw, start=1):
                if not isinstance(target, Mapping):
                    raise LockfileSchemaError(
                        f"skill {name!r} target #{index} must be an object"
                    )
                targets.append(dict(target))

        known = {
            "name",
            "source",
            "repo",
            "source_path",
            "declared_ref",
            "resolved_commit",
            "install_tree_sha",
            "bundle_sha256",
            "bundle_tree_sha256",
            "targets",
            "distributor_id",
            "version",
            "pinned_by",
            "pin_reason",
        }
        extras = {key: value for key, value in payload.items() if key not in known}

        return cls(
            name=name,
            source=source,
            repo=_coerce_optional_str(payload.get("repo"), f"skill {name!r}.repo"),
            source_path=_coerce_optional_str(payload.get("source_path"), f"skill {name!r}.source_path"),
            declared_ref=_coerce_optional_str(payload.get("declared_ref"), f"skill {name!r}.declared_ref"),
            resolved_commit=_coerce_optional_str(payload.get("resolved_commit"), f"skill {name!r}.resolved_commit"),
            install_tree_sha=_coerce_optional_str(payload.get("install_tree_sha"), f"skill {name!r}.install_tree_sha"),
            bundle_sha256=_coerce_optional_str(payload.get("bundle_sha256"), f"skill {name!r}.bundle_sha256"),
            bundle_tree_sha256=_coerce_optional_str(payload.get("bundle_tree_sha256"), f"skill {name!r}.bundle_tree_sha256"),
            targets=targets,
            distributor_id=_coerce_optional_str(payload.get("distributor_id"), f"skill {name!r}.distributor_id"),
            version=_coerce_optional_int(payload.get("version"), f"skill {name!r}.version"),
            pinned_by=_coerce_optional_str(payload.get("pinned_by"), f"skill {name!r}.pinned_by"),
            pin_reason=_coerce_optional_str(payload.get("pin_reason"), f"skill {name!r}.pin_reason"),
            extras=extras,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "source": self.source}
        if self.repo is not None:
            payload["repo"] = self.repo
        if self.source_path is not None:
            payload["source_path"] = self.source_path
        if self.declared_ref is not None:
            payload["declared_ref"] = self.declared_ref
        if self.resolved_commit is not None:
            payload["resolved_commit"] = self.resolved_commit
        if self.install_tree_sha is not None:
            payload["install_tree_sha"] = self.install_tree_sha
        if self.bundle_sha256 is not None:
            payload["bundle_sha256"] = self.bundle_sha256
        if self.bundle_tree_sha256 is not None:
            payload["bundle_tree_sha256"] = self.bundle_tree_sha256
        if self.targets:
            payload["targets"] = self.targets

        if self.distributor_id is not None:
            payload["distributor_id"] = self.distributor_id
        if self.version is not None:
            payload["version"] = self.version
        if self.pinned_by is not None:
            payload["pinned_by"] = self.pinned_by
        if self.pin_reason is not None:
            payload["pin_reason"] = self.pin_reason

        payload.update(self.extras)
        return payload


@dataclass(frozen=True)
class Lockfile:
    version: int
    config_sha: str
    synced_at: str
    skills: list[SkillLockEntry]
    distributor_manifests: dict[str, DistributorManifestLockEntry] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "config_sha": self.config_sha,
            "synced_at": self.synced_at,
            "distributor_manifests": {
                distributor_id: entry.to_dict()
                for distributor_id, entry in sorted(self.distributor_manifests.items())
            },
            "skills": [entry.to_dict() for entry in self.skills],
        }


def emit_lockfile(
    entries: Iterable[SkillLockEntry | Mapping[str, Any]],
    distributor_manifests: Mapping[str, DistributorManifestLockEntry | Mapping[str, Any]] | None = None,
    *,
    version: int = LOCKFILE_VERSION,
    config_sha: str,
    synced_at: str,
) -> dict[str, Any]:
    """Serialize lockfile entries into the distribution-aware lockfile shape."""
    skill_entries: list[SkillLockEntry] = []
    for entry in entries:
        if isinstance(entry, SkillLockEntry):
            skill_entries.append(entry)
            continue
        skill_entries.append(SkillLockEntry.from_dict(entry))

    manifest_entries: dict[str, DistributorManifestLockEntry] = {}
    for distributor_id, raw in (distributor_manifests or {}).items():
        if isinstance(raw, DistributorManifestLockEntry):
            manifest_entries[str(distributor_id)] = raw
        else:
            manifest_entries[str(distributor_id)] = DistributorManifestLockEntry.from_dict(
                str(distributor_id),
                raw,
            )

    lockfile = Lockfile(
        version=_coerce_int(version, "version"),
        config_sha=_coerce_str(config_sha, "config_sha"),
        synced_at=_coerce_str(synced_at, "synced_at"),
        skills=skill_entries,
        distributor_manifests=manifest_entries,
    )
    return lockfile.to_dict()


def parse_lockfile(raw: Mapping[str, Any]) -> Lockfile:
    """Parse a raw lockfile object with forward compatibility for legacy payloads."""
    if not isinstance(raw, Mapping):
        raise LockfileSchemaError("lockfile payload must be an object")

    version = _coerce_int(raw.get("version", LOCKFILE_VERSION), "version")
    config_sha = _coerce_optional_str(raw.get("config_sha"), "config_sha") or ""
    synced_at = _coerce_optional_str(raw.get("synced_at"), "synced_at") or ""

    raw_skills = raw.get("skills") or []
    if not isinstance(raw_skills, list):
        raise LockfileSchemaError("lockfile field 'skills' must be a list")
    skills = [SkillLockEntry.from_dict(item) for item in raw_skills]

    raw_manifests = raw.get("distributor_manifests") or {}
    if not isinstance(raw_manifests, Mapping):
        raise LockfileSchemaError("lockfile field 'distributor_manifests' must be an object")
    distributor_manifests = {
        str(distributor_id): DistributorManifestLockEntry.from_dict(
            str(distributor_id),
            payload,
        )
        for distributor_id, payload in raw_manifests.items()
    }

    return Lockfile(
        version=version,
        config_sha=config_sha,
        synced_at=synced_at,
        skills=skills,
        distributor_manifests=distributor_manifests,
    )


def _coerce_str(value: Any, field_name: str) -> str:
    if isinstance(value, str) and value.strip():
        return value
    raise LockfileSchemaError(f"{field_name} must be a non-empty string")


def _coerce_optional_str(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return text
    raise LockfileSchemaError(f"{field_name} must be a string when present")


def _coerce_int(value: Any, field_name: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise LockfileSchemaError(f"{field_name} must be an integer")


def _coerce_optional_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    return _coerce_int(value, field_name)


def _coerce_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise LockfileSchemaError(f"{field_name} must be a boolean")
