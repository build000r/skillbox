"""Local publisher for signed skill distribution artifacts."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from ..shared import atomic_write_text
from .bundle import BundleError, pack_skill_bundle, _file_sha256
from .manifest import (
    MANIFEST_INVALID_CODE,
    ClientManifestArtifact,
    ManifestSchemaError,
    parse_manifest,
    verify_manifest,
)
from .signing import KEY_PREFIX, SignatureVerificationError, sign_manifest

DISTRIBUTION_SKILL_METADATA_MISSING = "DISTRIBUTION_SKILL_METADATA_MISSING"
DISTRIBUTION_VERSION_CONFLICT = "DISTRIBUTION_VERSION_CONFLICT"
SKILL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class DistributionPublishError(RuntimeError):
    pass


def publish_skill_release(
    *,
    skill_path: Path,
    version: int,
    manifest_path: Path,
    artifact_root: Path,
    signing_key_ref: str,
    distributor_id: str,
    client_id: str,
    skill_name: str | None = None,
    targets: list[str] | None = None,
    capabilities: list[str] | None = None,
    changelog: str | None = None,
    min_version: int | None = None,
    min_version_reason: str | None = None,
    download_prefix: str = "/skills",
    updated_at: str | None = None,
) -> dict[str, Any]:
    """Publish one skill version into a local artifact root and signed v2 manifest."""
    prepared = _prepare_skill_release(
        skill_path=skill_path,
        version=version,
        skill_name=skill_name,
        targets=targets,
        capabilities=capabilities,
        artifact_root=artifact_root,
        download_prefix=download_prefix,
        updated_at=updated_at,
        signing_key_ref=signing_key_ref,
    )

    with tempfile.TemporaryDirectory(prefix=f"skillbox-publish-{prepared['name']}-") as tmp_str:
        return _publish_prepared_skill_release(
            prepared=prepared,
            version=version,
            manifest_path=manifest_path,
            distributor_id=distributor_id,
            client_id=client_id,
            changelog=changelog,
            min_version=min_version,
            min_version_reason=min_version_reason,
            tmp=Path(tmp_str),
        )


def _prepare_skill_release(
    *,
    skill_path: Path,
    version: int,
    skill_name: str | None,
    targets: list[str] | None,
    capabilities: list[str] | None,
    artifact_root: Path,
    download_prefix: str,
    updated_at: str | None,
    signing_key_ref: str,
) -> dict[str, Any]:
    if version < 0:
        raise DistributionPublishError("version must be >= 0")

    skill_path = Path(skill_path).resolve()
    if not skill_path.is_dir() or not (skill_path / "SKILL.md").is_file():
        raise DistributionPublishError(
            f"{DISTRIBUTION_SKILL_METADATA_MISSING}: skill path must contain SKILL.md"
        )

    name = (skill_name or skill_path.name).strip()
    _validate_skill_name(name)

    publish_targets = targets or []
    publish_capabilities = capabilities or []
    now = updated_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    private_key = _load_private_key_ref(signing_key_ref)

    artifact_dir = Path(artifact_root).resolve() / "skills" / name / str(version)
    final_bundle = artifact_dir / "bundle.tar.gz"
    download_url = f"{download_prefix.rstrip('/')}/{name}/{version}/bundle.tar.gz"
    return {
        "skill_path": skill_path,
        "name": name,
        "targets": publish_targets,
        "capabilities": publish_capabilities,
        "updated_at": now,
        "private_key": private_key,
        "artifact_dir": artifact_dir,
        "final_bundle": final_bundle,
        "download_url": download_url,
    }


def _validate_skill_name(name: str) -> None:
    if not name:
        raise DistributionPublishError("skill_name must be non-empty")
    if (
        not SKILL_NAME_PATTERN.fullmatch(name)
        or name in {".", ".."}
        or ".." in name.split(".")
        or "/" in name
        or "\\" in name
        or Path(name).name != name
    ):
        raise DistributionPublishError(
            "skill_name must be a slug using letters, numbers, dots, underscores, or hyphens"
        )


def _packed_skill_bundle(skill_path: Path, version: int, name: str, tmp: Path) -> Path:
    try:
        return pack_skill_bundle(skill_path, version, name=name, output_dir=tmp)
    except BundleError as exc:
        raise DistributionPublishError(str(exc)) from exc


def _existing_artifact_or_conflict(
    skill_entry: dict[str, Any] | None,
    version: int,
    name: str,
    artifact_sha: str,
) -> dict[str, Any] | None:
    existing_artifact = _find_artifact_entry(skill_entry, version) if skill_entry else None
    if existing_artifact and str(existing_artifact.get("sha256")) != artifact_sha:
        raise DistributionPublishError(
            f"{DISTRIBUTION_VERSION_CONFLICT}: {name} v{version} already has different artifact bytes"
        )
    return existing_artifact


def _publish_artifact_changed(
    existing_artifact: dict[str, Any] | None,
    final_bundle: Path,
    artifact_sha: str,
) -> bool:
    return not (
        existing_artifact
        and str(existing_artifact.get("sha256")) == artifact_sha
        and final_bundle.is_file()
        and _file_sha256(final_bundle) == artifact_sha
    )


def _write_changed_skill_release(
    *,
    prepared: dict[str, Any],
    version: int,
    artifact_sha: str,
    artifact_size: int,
    tmp_bundle: Path,
    manifest_data: dict[str, Any],
    manifest_path: Path,
    changelog: str | None,
    min_version: int | None,
    min_version_reason: str | None,
) -> dict[str, Any]:
    prepared["artifact_dir"].mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tmp_bundle, prepared["final_bundle"])
    manifest_data = _upsert_manifest_skill(
        manifest_data=manifest_data,
        name=prepared["name"],
        version=version,
        sha256=artifact_sha,
        size_bytes=artifact_size,
        download_url=prepared["download_url"],
        targets=prepared["targets"],
        capabilities=prepared["capabilities"],
        changelog=changelog,
        min_version=min_version,
        min_version_reason=min_version_reason,
        updated_at=prepared["updated_at"],
    )
    signed_manifest = sign_manifest(manifest_data, prepared["private_key"])
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(manifest_path, json.dumps(signed_manifest, indent=2, sort_keys=True) + "\n")
    return manifest_data


def _candidate_manifest_skill(
    *,
    prepared: dict[str, Any],
    version: int,
    artifact_sha: str,
    artifact_size: int,
    manifest_data: dict[str, Any],
    changelog: str | None,
    min_version: int | None,
    min_version_reason: str | None,
) -> dict[str, Any] | None:
    candidate = _upsert_manifest_skill(
        manifest_data=manifest_data,
        name=prepared["name"],
        version=version,
        sha256=artifact_sha,
        size_bytes=artifact_size,
        download_url=prepared["download_url"],
        targets=prepared["targets"],
        capabilities=prepared["capabilities"],
        changelog=changelog,
        min_version=min_version,
        min_version_reason=min_version_reason,
        updated_at=prepared["updated_at"],
    )
    return _find_skill_entry(candidate, prepared["name"])


def _publish_manifest_changed(
    *,
    prepared: dict[str, Any],
    version: int,
    artifact_sha: str,
    artifact_size: int,
    manifest_data: dict[str, Any],
    changelog: str | None,
    min_version: int | None,
    min_version_reason: str | None,
) -> bool:
    existing_skill = _find_skill_entry(manifest_data, prepared["name"])
    candidate_skill = _candidate_manifest_skill(
        prepared=prepared,
        version=version,
        artifact_sha=artifact_sha,
        artifact_size=artifact_size,
        manifest_data=manifest_data,
        changelog=changelog,
        min_version=min_version,
        min_version_reason=min_version_reason,
    )
    return candidate_skill != existing_skill


def _publish_prepared_skill_release(
    *,
    prepared: dict[str, Any],
    version: int,
    manifest_path: Path,
    distributor_id: str,
    client_id: str,
    changelog: str | None,
    min_version: int | None,
    min_version_reason: str | None,
    tmp: Path,
) -> dict[str, Any]:
    tmp_bundle = _packed_skill_bundle(prepared["skill_path"], version, prepared["name"], tmp)
    artifact_sha = _file_sha256(tmp_bundle)
    artifact_size = tmp_bundle.stat().st_size
    manifest_data = _load_or_create_manifest(
        manifest_path=manifest_path,
        distributor_id=distributor_id,
        client_id=client_id,
        updated_at=prepared["updated_at"],
        public_key=prepared["private_key"].public_key(),
    )
    skill_entry = _find_skill_entry(manifest_data, prepared["name"])
    existing_artifact = _existing_artifact_or_conflict(
        skill_entry, version, prepared["name"], artifact_sha,
    )
    artifact_changed = _publish_artifact_changed(
        existing_artifact, prepared["final_bundle"], artifact_sha,
    )
    manifest_changed = _publish_manifest_changed(
        prepared=prepared,
        version=version,
        artifact_sha=artifact_sha,
        artifact_size=artifact_size,
        manifest_data=manifest_data,
        changelog=changelog,
        min_version=min_version,
        min_version_reason=min_version_reason,
    )
    changed = artifact_changed or manifest_changed
    if changed:
        manifest_data = _write_changed_skill_release(
            prepared=prepared,
            version=version,
            artifact_sha=artifact_sha,
            artifact_size=artifact_size,
            tmp_bundle=tmp_bundle,
            manifest_data=manifest_data,
            manifest_path=manifest_path,
            changelog=changelog,
            min_version=min_version,
            min_version_reason=min_version_reason,
        )
    return {
        "ok": True,
        "result": "published" if changed else "noop",
        "skill": prepared["name"],
        "version": version,
        "artifact_path": str(prepared["final_bundle"]),
        "artifact_sha256": artifact_sha,
        "size_bytes": artifact_size,
        "download_url": prepared["download_url"],
        "manifest_path": str(manifest_path),
        "manifest_version": int(manifest_data["manifest_version"]),
        "signature_state": "signed",
    }


def _load_or_create_manifest(
    *,
    manifest_path: Path,
    distributor_id: str,
    client_id: str,
    updated_at: str,
    public_key: Any,
) -> dict[str, Any]:
    if not manifest_path.is_file():
        return {
            "schema_version": 2,
            "distributor_id": distributor_id,
            "client_id": client_id,
            "manifest_version": 0,
            "updated_at": updated_at,
            "skills": [],
        }
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = parse_manifest(json.dumps(raw).encode("utf-8"))
        verify_manifest(manifest, public_key)
    except (json.JSONDecodeError, UnicodeDecodeError, ManifestSchemaError, SignatureVerificationError) as exc:
        raise DistributionPublishError(f"{MANIFEST_INVALID_CODE}: {exc}") from exc
    if manifest.schema_version != 2:
        raise DistributionPublishError(
            f"{MANIFEST_INVALID_CODE}: publisher requires schema_version 2"
        )
    if manifest.distributor_id != distributor_id:
        raise DistributionPublishError(
            f"{MANIFEST_INVALID_CODE}: manifest distributor_id does not match"
        )
    if manifest.client_id != client_id:
        raise DistributionPublishError(
            f"{MANIFEST_INVALID_CODE}: manifest client_id does not match"
        )
    return raw


def _find_skill_entry(manifest_data: dict[str, Any], name: str) -> dict[str, Any] | None:
    for entry in manifest_data.get("skills") or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def _find_artifact_entry(
    skill_entry: dict[str, Any] | None,
    version: int,
) -> dict[str, Any] | None:
    if not skill_entry:
        return None
    for artifact in skill_entry.get("artifacts") or []:
        if isinstance(artifact, dict) and artifact.get("version") == version:
            return artifact
    return None


def _next_manifest_base(manifest_data: dict[str, Any], updated_at: str) -> dict[str, Any]:
    next_manifest = dict(manifest_data)
    next_manifest["manifest_version"] = int(next_manifest.get("manifest_version") or 0) + 1
    next_manifest["updated_at"] = updated_at
    return next_manifest


def _manifest_skills_without(manifest_data: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [
        dict(skill)
        for skill in (manifest_data.get("skills") or [])
        if isinstance(skill, dict) and skill.get("name") != name
    ]


def _manifest_artifacts_without(existing: dict[str, Any], version: int) -> list[dict[str, Any]]:
    return [
        dict(artifact)
        for artifact in (existing.get("artifacts") or [])
        if isinstance(artifact, dict) and artifact.get("version") != version
    ]


def _client_manifest_artifact_payload(
    *,
    version: int,
    sha256: str,
    size_bytes: int,
    download_url: str,
    changelog: str | None,
) -> dict[str, Any]:
    artifact = ClientManifestArtifact(
        version=version,
        sha256=sha256,
        size_bytes=size_bytes,
        download_url=download_url,
        changelog=changelog,
    )
    payload: dict[str, Any] = {
        "version": artifact.version,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "download_url": artifact.download_url,
    }
    if artifact.changelog:
        payload["changelog"] = artifact.changelog
    return payload


def _manifest_skill_payload(
    *,
    name: str,
    version: int,
    artifacts: list[dict[str, Any]],
    targets: list[str],
    capabilities: list[str],
    changelog: str | None,
    min_version: int | None,
    min_version_reason: str | None,
    existing: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "recommended_version": version,
        "targets": targets or list(existing.get("targets") or ["box"]),
        "artifacts": artifacts,
    }
    selected_min = min_version if min_version is not None else existing.get("min_version")
    if selected_min is not None:
        payload["min_version"] = selected_min
    selected_reason = min_version_reason or existing.get("min_version_reason")
    if selected_reason:
        payload["min_version_reason"] = selected_reason
    selected_capabilities = capabilities or list(existing.get("capabilities") or [])
    if selected_capabilities:
        payload["capabilities"] = selected_capabilities
    if changelog:
        payload["changelog"] = changelog
    return payload


def _upsert_manifest_skill(
    *,
    manifest_data: dict[str, Any],
    name: str,
    version: int,
    sha256: str,
    size_bytes: int,
    download_url: str,
    targets: list[str],
    capabilities: list[str],
    changelog: str | None,
    min_version: int | None,
    min_version_reason: str | None,
    updated_at: str,
) -> dict[str, Any]:
    next_manifest = _next_manifest_base(manifest_data, updated_at)
    skills = _manifest_skills_without(manifest_data, name)
    existing = _find_skill_entry(manifest_data, name) or {}
    existing_artifact = _find_artifact_entry(existing, version) or {}
    selected_changelog = changelog if changelog is not None else existing_artifact.get("changelog")
    artifacts = _manifest_artifacts_without(existing, version)
    artifacts.append(_client_manifest_artifact_payload(
        version=version,
        sha256=sha256,
        size_bytes=size_bytes,
        download_url=download_url,
        changelog=selected_changelog,
    ))
    artifacts.sort(key=lambda item: int(item["version"]))

    skills.append(_manifest_skill_payload(
        name=name,
        version=version,
        artifacts=artifacts,
        targets=targets,
        capabilities=capabilities,
        changelog=changelog if changelog is not None else existing.get("changelog"),
        min_version=min_version,
        min_version_reason=min_version_reason,
        existing=existing,
    ))
    skills.sort(key=lambda item: str(item.get("name") or ""))
    next_manifest["skills"] = skills
    return next_manifest


def _load_private_key_ref(ref: str) -> Ed25519PrivateKey:
    text = _load_key_ref_text(ref)
    if text.startswith(KEY_PREFIX):
        raw = base64.b64decode(text[len(KEY_PREFIX):], validate=True)
        if len(raw) != 32:
            raise DistributionPublishError("ed25519 private key must be 32 bytes")
        return Ed25519PrivateKey.from_private_bytes(raw)
    try:
        key = load_pem_private_key(text.encode("utf-8"), password=None)
    except Exception as exc:
        raise DistributionPublishError("failed to load Ed25519 signing key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise DistributionPublishError("signing key must be an Ed25519 private key")
    return key


def _load_key_ref_text(ref: str) -> str:
    if ref.startswith("env:"):
        env_name = ref[len("env:"):].strip()
        value = os.environ.get(env_name)
        if not value:
            raise DistributionPublishError(f"signing key env var {env_name!r} is not set")
        return value.strip()
    path_text = ref[len("file:"):] if ref.startswith("file:") else ref
    path = Path(path_text).expanduser()
    if not path.is_file():
        raise DistributionPublishError(f"signing key file not found: {path}")
    return path.read_text(encoding="utf-8").strip()
