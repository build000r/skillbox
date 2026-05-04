"""Per-client manifest schema for skillbox distribution.

A client manifest is a signed JSON document served by a distributor to each
client.  It declares which skills are available, at which versions, and with
what constraints (min_version floors, target filters).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .signing import Ed25519PublicKey, verify_manifest_signature

SUPPORTED_SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {1, 2}
MANIFEST_INVALID_CODE = "DISTRIBUTION_MANIFEST_INVALID"


class ManifestSchemaError(Exception):
    pass


@dataclass(frozen=True)
class ClientManifestArtifact:
    version: int
    sha256: str
    size_bytes: int
    download_url: str
    changelog: str | None = None


@dataclass(frozen=True)
class ClientManifestSkill:
    name: str
    version: int
    sha256: str
    size_bytes: int
    download_url: str
    targets: list[str]
    min_version: int | None = None
    min_version_reason: str | None = None
    changelog: str | None = None
    recommended_version: int | None = None
    artifacts: list[ClientManifestArtifact] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ClientManifest:
    schema_version: int
    distributor_id: str
    client_id: str
    manifest_version: int
    updated_at: str
    skills: list[ClientManifestSkill]
    raw: dict[str, Any] = field(repr=False)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _require_str(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestSchemaError(f"{label}.{key} must be a non-empty string")
    return value


def _require_int(data: dict[str, Any], key: str, label: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestSchemaError(f"{label}.{key} must be an integer")
    return value


def _optional_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _manifest_invalid(message: str) -> ManifestSchemaError:
    return ManifestSchemaError(f"{MANIFEST_INVALID_CODE}: {message}")


def _optional_int_strict(data: dict[str, Any], key: str, label: str) -> int | None:
    if key not in data or data.get(key) is None:
        return None
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestSchemaError(f"{label}.{key} must be an integer when present")
    return value


def _parse_targets(raw: dict[str, Any], label: str) -> list[str]:
    raw_targets = raw.get("targets")
    if not isinstance(raw_targets, list):
        raise ManifestSchemaError(f"{label}.targets must be a list")
    return [str(t) for t in raw_targets]


def _optional_str_list(data: dict[str, Any], key: str, label: str) -> list[str]:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ManifestSchemaError(f"{label}.{key} must be a list when present")
    return [str(item) for item in value]


def _parse_artifact(raw: Any, skill_label: str, index: int) -> ClientManifestArtifact:
    label = f"{skill_label}.artifacts[{index}]"
    if not isinstance(raw, dict):
        raise ManifestSchemaError(f"{label} must be an object")
    return ClientManifestArtifact(
        version=_require_int(raw, "version", label),
        sha256=_require_str(raw, "sha256", label),
        size_bytes=_require_int(raw, "size_bytes", label),
        download_url=_require_str(raw, "download_url", label),
        changelog=_optional_str(raw, "changelog"),
    )


def artifact_for_version(
    skill: ClientManifestSkill,
    version: int,
) -> ClientManifestArtifact | None:
    for artifact in skill.artifacts:
        if artifact.version == version:
            return artifact
    return None


def _parse_skill_v1(raw: Any, index: int) -> ClientManifestSkill:
    label = f"skills[{index}]"
    if not isinstance(raw, dict):
        raise ManifestSchemaError(f"{label} must be an object")

    name = _require_str(raw, "name", label)
    version = _require_int(raw, "version", label)
    sha256 = _require_str(raw, "sha256", label)
    size_bytes = _require_int(raw, "size_bytes", label)
    download_url = _require_str(raw, "download_url", label)

    targets = _parse_targets(raw, label)

    return ClientManifestSkill(
        name=name,
        version=version,
        sha256=sha256,
        size_bytes=size_bytes,
        download_url=download_url,
        targets=targets,
        min_version=_optional_int(raw, "min_version"),
        min_version_reason=_optional_str(raw, "min_version_reason"),
        changelog=_optional_str(raw, "changelog"),
        recommended_version=version,
    )


def _parse_skill_v2(raw: Any, index: int) -> ClientManifestSkill:
    label = f"skills[{index}]"
    if not isinstance(raw, dict):
        raise ManifestSchemaError(f"{label} must be an object")

    name = _require_str(raw, "name", label)
    recommended_version = _require_int(raw, "recommended_version", label)
    targets = _parse_targets(raw, label)
    min_version = _optional_int_strict(raw, "min_version", label)

    raw_artifacts = raw.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise ManifestSchemaError(f"{label}.artifacts must be a non-empty list")
    artifacts = [_parse_artifact(a, label, i) for i, a in enumerate(raw_artifacts)]
    artifact_versions = {artifact.version for artifact in artifacts}
    if len(artifact_versions) != len(artifacts):
        raise _manifest_invalid(f"{label}.artifacts must not contain duplicate versions")

    recommended_artifact = next(
        (artifact for artifact in artifacts if artifact.version == recommended_version),
        None,
    )
    if recommended_artifact is None:
        raise _manifest_invalid(
            f"{label}.recommended_version {recommended_version} has no artifact"
        )
    if min_version is not None and min_version not in artifact_versions:
        raise _manifest_invalid(f"{label}.min_version {min_version} has no artifact")

    return ClientManifestSkill(
        name=name,
        version=recommended_version,
        sha256=recommended_artifact.sha256,
        size_bytes=recommended_artifact.size_bytes,
        download_url=recommended_artifact.download_url,
        targets=targets,
        min_version=min_version,
        min_version_reason=_optional_str(raw, "min_version_reason"),
        changelog=_optional_str(raw, "changelog") or recommended_artifact.changelog,
        recommended_version=recommended_version,
        artifacts=artifacts,
        capabilities=_optional_str_list(raw, "capabilities", label),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_manifest(raw_bytes: bytes) -> ClientManifest:
    try:
        data = json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ManifestSchemaError(f"invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ManifestSchemaError("manifest must be a JSON object")

    schema_version = _require_int(data, "schema_version", "manifest")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ManifestSchemaError(
            f"unsupported schema_version {schema_version}; "
            f"expected one of {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )

    distributor_id = _require_str(data, "distributor_id", "manifest")
    client_id = _require_str(data, "client_id", "manifest")
    manifest_version = _require_int(data, "manifest_version", "manifest")
    updated_at = _require_str(data, "updated_at", "manifest")

    raw_skills = data.get("skills")
    if not isinstance(raw_skills, list):
        raise ManifestSchemaError("manifest.skills must be a list")
    if schema_version == 1:
        skills = [_parse_skill_v1(s, i) for i, s in enumerate(raw_skills)]
    else:
        skills = [_parse_skill_v2(s, i) for i, s in enumerate(raw_skills)]

    return ClientManifest(
        schema_version=schema_version,
        distributor_id=distributor_id,
        client_id=client_id,
        manifest_version=manifest_version,
        updated_at=updated_at,
        skills=skills,
        raw=data,
    )


def verify_manifest(manifest: ClientManifest, public_key: Ed25519PublicKey) -> None:
    verify_manifest_signature(manifest.raw, public_key)


def filter_skills_for_target(
    manifest: ClientManifest,
    target: str,
) -> list[ClientManifestSkill]:
    return [s for s in manifest.skills if target in s.targets]
