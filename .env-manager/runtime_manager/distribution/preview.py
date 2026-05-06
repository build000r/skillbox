"""Read-only install preview for skill distribution manifests."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bundle import _file_sha256
from .lockfile import Lockfile, SkillLockEntry, parse_lockfile
from .manifest import ClientManifest, ClientManifestSkill, parse_manifest, verify_manifest
from .pin_resolver import PinResolution, PinResolutionError, resolve_pin
from .signing import KeyFormatError, SignatureVerificationError, load_public_key


class DistributionPreviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class DistributionPlanItem:
    skill: str
    action: str
    selected_version: int | None
    artifact_sha256: str | None
    download_url: str | None
    pinned_by: str | None
    cache_state: str
    signature_state: str
    targets: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    changelog: str | None = None
    pin_reason: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "skill": self.skill,
            "action": self.action,
            "selected_version": self.selected_version,
            "artifact_sha256": self.artifact_sha256,
            "download_url": self.download_url,
            "pinned_by": self.pinned_by,
            "cache_state": self.cache_state,
            "signature_state": self.signature_state,
            "targets": list(self.targets),
            "capabilities": list(self.capabilities),
            "warnings": list(self.warnings),
        }
        if self.changelog is not None:
            payload["changelog"] = self.changelog
        if self.pin_reason is not None:
            payload["pin_reason"] = self.pin_reason
        return payload


def preview_manifest(
    *,
    manifest_bytes: bytes,
    public_key_config: str,
    distributor_id: str,
    state_root: Path,
    pick: list[str] | None = None,
    pin: dict[str, int] | None = None,
    target_env: str | None = None,
    lockfile_path: Path | None = None,
) -> dict[str, Any]:
    """Build a read-only selected-artifact plan from a signed manifest."""
    try:
        manifest = parse_manifest(manifest_bytes)
    except Exception as exc:
        raise DistributionPreviewError(str(exc)) from exc

    signature_state = "verified"
    try:
        verify_manifest(manifest, load_public_key(public_key_config))
    except KeyFormatError as exc:
        signature_state = "invalid"
        return {
            "ok": True,
            "ready": False,
            "distributor_id": distributor_id,
            "manifest_version": manifest.manifest_version,
            "items": [],
            "warnings": [f"manifest public key invalid: {exc}"],
            "signature_state": signature_state,
        }
    except SignatureVerificationError as exc:
        signature_state = "invalid"
        return {
            "ok": True,
            "ready": False,
            "distributor_id": distributor_id,
            "manifest_version": manifest.manifest_version,
            "items": [],
            "warnings": [f"manifest signature verification failed: {exc}"],
            "signature_state": signature_state,
        }

    if manifest.distributor_id != distributor_id:
        return {
            "ok": True,
            "ready": False,
            "distributor_id": distributor_id,
            "manifest_version": manifest.manifest_version,
            "items": [],
            "warnings": [
                (
                    f"manifest distributor_id {manifest.distributor_id!r} does not "
                    f"match {distributor_id!r}"
                )
            ],
            "signature_state": "verified",
        }

    lockfile = _read_lockfile(lockfile_path)
    items = plan_manifest_items(
        manifest=manifest,
        state_root=state_root,
        pick=pick,
        pin=pin,
        target_env=target_env,
        lockfile=lockfile,
        signature_state=signature_state,
    )
    ready = all(item.action != "blocked" for item in items)
    return {
        "ok": True,
        "ready": ready,
        "distributor_id": distributor_id,
        "manifest_version": manifest.manifest_version,
        "items": [item.to_dict() for item in items],
        "warnings": [],
        "signature_state": signature_state,
    }


def plan_manifest_items(
    *,
    manifest: ClientManifest,
    state_root: Path,
    pick: list[str] | None = None,
    pin: dict[str, int] | None = None,
    target_env: str | None = None,
    lockfile: Lockfile | None = None,
    signature_state: str = "verified",
) -> list[DistributionPlanItem]:
    skills = list(manifest.skills)
    if pick:
        pick_set = set(pick)
        skills = [skill for skill in skills if skill.name in pick_set]
    if target_env:
        skills = [skill for skill in skills if target_env in skill.targets]

    return [
        _plan_skill_item(
            skill=skill,
            resolution=_resolve_or_none(skill, (pin or {}).get(skill.name)),
            state_root=state_root,
            lock_entry=_find_lock_entry(lockfile, manifest.distributor_id, skill.name),
            signature_state=signature_state,
        )
        for skill in skills
    ]


def _resolve_or_none(
    skill: ClientManifestSkill,
    client_pin: int | None,
) -> PinResolution | PinResolutionError:
    try:
        return resolve_pin(skill, client_pin)
    except PinResolutionError as exc:
        return exc


def _plan_skill_item(
    *,
    skill: ClientManifestSkill,
    resolution: PinResolution | PinResolutionError,
    state_root: Path,
    lock_entry: SkillLockEntry | None,
    signature_state: str,
) -> DistributionPlanItem:
    if isinstance(resolution, PinResolutionError):
        return DistributionPlanItem(
            skill=skill.name,
            action="blocked",
            selected_version=None,
            artifact_sha256=None,
            download_url=None,
            pinned_by=None,
            cache_state="unknown",
            signature_state=signature_state,
            targets=skill.targets,
            capabilities=skill.capabilities,
            warnings=[str(resolution)],
        )

    artifact = resolution.artifact
    artifact_sha = artifact.sha256 if artifact else skill.sha256
    download_url = artifact.download_url if artifact else skill.download_url
    changelog = artifact.changelog if artifact and artifact.changelog else skill.changelog
    cache_state = _cache_state(state_root, skill.name, resolution.version, artifact_sha)
    action = _plan_action(resolution, lock_entry, artifact_sha, cache_state)

    return DistributionPlanItem(
        skill=skill.name,
        action=action,
        selected_version=resolution.version,
        artifact_sha256=artifact_sha,
        download_url=download_url,
        pinned_by=resolution.pinned_by,
        pin_reason=resolution.reason,
        cache_state=cache_state,
        signature_state=signature_state,
        targets=skill.targets,
        capabilities=skill.capabilities,
        changelog=changelog,
    )


def _plan_action(
    resolution: PinResolution,
    lock_entry: SkillLockEntry | None,
    artifact_sha: str,
    cache_state: str,
) -> str:
    if cache_state == "mismatch":
        return "blocked"
    if resolution.pinned_by == "manifest_floor":
        return "floor_override"
    if lock_entry is None or lock_entry.version is None:
        return "install"
    if lock_entry.version == resolution.version and lock_entry.bundle_sha256 == artifact_sha:
        return "unchanged"
    if lock_entry.version > resolution.version and cache_state == "cached":
        return "rollback_available"
    return "update"


def _cache_state(
    state_root: Path,
    skill_name: str,
    version: int,
    expected_sha: str,
) -> str:
    cached = Path(state_root) / "bundle-cache" / skill_name / (
        f"{skill_name}-v{version}.skillbundle.tar.gz"
    )
    if not cached.is_file():
        return "missing"
    actual = _file_sha256(cached)
    return "cached" if actual == expected_sha else "mismatch"


def _find_lock_entry(
    lockfile: Lockfile | None,
    distributor_id: str,
    skill_name: str,
) -> SkillLockEntry | None:
    if not lockfile:
        return None
    for entry in lockfile.skills:
        if (
            entry.source == "distributor"
            and entry.distributor_id == distributor_id
            and entry.name == skill_name
        ):
            return entry
    return None


def _read_lockfile(lockfile_path: Path | None) -> Lockfile | None:
    if not lockfile_path or not Path(lockfile_path).is_file():
        return None
    try:
        raw = json.loads(Path(lockfile_path).read_text(encoding="utf-8"))
        return parse_lockfile(raw)
    except Exception as exc:
        raise DistributionPreviewError(f"failed to read lockfile: {exc}") from exc
