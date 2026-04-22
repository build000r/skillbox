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

SUPPORTED_SCHEMA_VERSION = 1


class ManifestSchemaError(Exception):
    pass


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


def _parse_skill(raw: Any, index: int) -> ClientManifestSkill:
    label = f"skills[{index}]"
    if not isinstance(raw, dict):
        raise ManifestSchemaError(f"{label} must be an object")

    name = _require_str(raw, "name", label)
    version = _require_int(raw, "version", label)
    sha256 = _require_str(raw, "sha256", label)
    size_bytes = _require_int(raw, "size_bytes", label)
    download_url = _require_str(raw, "download_url", label)

    raw_targets = raw.get("targets")
    if not isinstance(raw_targets, list):
        raise ManifestSchemaError(f"{label}.targets must be a list")
    targets = [str(t) for t in raw_targets]

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
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ManifestSchemaError(
            f"unsupported schema_version {schema_version}; expected {SUPPORTED_SCHEMA_VERSION}"
        )

    distributor_id = _require_str(data, "distributor_id", "manifest")
    client_id = _require_str(data, "client_id", "manifest")
    manifest_version = _require_int(data, "manifest_version", "manifest")
    updated_at = _require_str(data, "updated_at", "manifest")

    raw_skills = data.get("skills")
    if not isinstance(raw_skills, list):
        raise ManifestSchemaError("manifest.skills must be a list")
    skills = [_parse_skill(s, i) for i, s in enumerate(raw_skills)]

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
