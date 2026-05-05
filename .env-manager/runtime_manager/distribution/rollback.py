"""Rollback from the signed distribution bundle cache."""
from __future__ import annotations

import datetime
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..shared import atomic_write_text
from .bundle import SKILL_META_DIR, unpack_skill_bundle, verify_bundle_contents, _file_sha256
from .lockfile import SkillLockEntry, emit_lockfile, parse_lockfile
from .manifest import artifact_for_version, parse_manifest, verify_manifest
from .signing import SignatureVerificationError, load_public_key
from .sync import _install_skill_content

DISTRIBUTION_CACHE_MISSING = "DISTRIBUTION_CACHE_MISSING"
DISTRIBUTION_CACHE_MISMATCH = "DISTRIBUTION_CACHE_MISMATCH"
DISTRIBUTION_FLOOR_VIOLATION = "DISTRIBUTION_FLOOR_VIOLATION"


class DistributionRollbackError(RuntimeError):
    pass


def cached_versions(
    *,
    state_root: Path,
    skill_name: str,
) -> list[int]:
    cache_dir = Path(state_root) / "bundle-cache" / skill_name
    if not cache_dir.is_dir():
        return []
    prefix = f"{skill_name}-v"
    suffix = ".skillbundle.tar.gz"
    versions: list[int] = []
    for path in cache_dir.iterdir():
        name = path.name
        if not path.is_file() or not name.startswith(prefix) or not name.endswith(suffix):
            continue
        raw_version = name[len(prefix):-len(suffix)]
        try:
            versions.append(int(raw_version))
        except ValueError:
            continue
    return sorted(set(versions))


def rollback_distributor_skill(
    *,
    manifest_path: Path,
    public_key_config: str,
    distributor_id: str,
    skill_name: str,
    target_version: int,
    state_root: Path,
    install_targets: list[dict[str, Any]],
    lockfile_path: Path,
    reason: str | None = None,
    emergency_override: bool = False,
) -> dict[str, Any]:
    """Install a previously cached signed bundle version and update the lockfile."""
    manifest = _verified_rollback_manifest(manifest_path, public_key_config, distributor_id)
    skill = _rollback_skill_entry(manifest, skill_name, target_version, emergency_override)
    artifact = _rollback_artifact_entry(skill, skill_name, target_version)
    cached_bundle = _cached_rollback_bundle(state_root, skill_name, target_version, artifact.sha256)
    install_tree_sha, bundle_tree_sha = _install_cached_rollback_bundle(
        cached_bundle, skill_name, install_targets,
    )
    from_version = _current_lock_version(
        lockfile_path=lockfile_path,
        distributor_id=distributor_id,
        skill_name=skill_name,
    )
    _write_rollback_lockfile(
        lockfile_path=lockfile_path,
        distributor_id=distributor_id,
        skill_name=skill_name,
        target_version=target_version,
        artifact=artifact,
        bundle_tree_sha=bundle_tree_sha,
        install_tree_sha=install_tree_sha,
        reason=reason,
        emergency_override=emergency_override,
        manifest_version=manifest.manifest_version,
        changelog=artifact.changelog or skill.changelog,
    )
    return {
        "ok": True,
        "skill": skill_name,
        "from_version": from_version,
        "to_version": target_version,
        "source": "bundle-cache",
        "bundle_sha256": artifact.sha256,
        "lockfile_updated": True,
        "pinned_by": "rollback",
        "reason": reason,
        "emergency_override": emergency_override,
        "cached_versions": cached_versions(state_root=state_root, skill_name=skill_name),
    }


def _verified_rollback_manifest(
    manifest_path: Path,
    public_key_config: str,
    distributor_id: str,
) -> Any:
    manifest = parse_manifest(Path(manifest_path).read_bytes())
    try:
        verify_manifest(manifest, load_public_key(public_key_config))
    except SignatureVerificationError as exc:
        raise DistributionRollbackError(f"manifest signature verification failed: {exc}") from exc
    if manifest.distributor_id != distributor_id:
        raise DistributionRollbackError("manifest distributor_id does not match")
    return manifest


def _rollback_skill_entry(
    manifest: Any,
    skill_name: str,
    target_version: int,
    emergency_override: bool,
) -> Any:
    skill = next((item for item in manifest.skills if item.name == skill_name), None)
    if skill is None:
        raise DistributionRollbackError(f"skill {skill_name!r} not found in manifest")
    if skill.min_version is not None and target_version < skill.min_version and not emergency_override:
        raise DistributionRollbackError(
            f"{DISTRIBUTION_FLOOR_VIOLATION}: {skill_name} v{target_version} is "
            f"below manifest floor v{skill.min_version}"
        )
    return skill


def _rollback_artifact_entry(skill: Any, skill_name: str, target_version: int) -> Any:
    artifact = artifact_for_version(skill, target_version)
    if artifact is None:
        raise DistributionRollbackError(
            f"DISTRIBUTION_ARTIFACT_NOT_AVAILABLE: {skill_name} v{target_version}"
        )
    return artifact


def _cached_rollback_bundle(
    state_root: Path,
    skill_name: str,
    target_version: int,
    expected_sha256: str,
) -> Path:
    cached_bundle = Path(state_root) / "bundle-cache" / skill_name / (
        f"{skill_name}-v{target_version}.skillbundle.tar.gz"
    )
    if not cached_bundle.is_file():
        raise DistributionRollbackError(f"{DISTRIBUTION_CACHE_MISSING}: {cached_bundle}")
    actual_sha = _file_sha256(cached_bundle)
    if actual_sha != expected_sha256:
        raise DistributionRollbackError(
            f"{DISTRIBUTION_CACHE_MISMATCH}: expected {expected_sha256}, got {actual_sha}"
        )
    return cached_bundle


def _install_cached_rollback_bundle(
    cached_bundle: Path,
    skill_name: str,
    install_targets: list[dict[str, Any]],
) -> tuple[str | None, str]:
    install_tree_sha: str | None = None
    with tempfile.TemporaryDirectory(prefix=f"skillbox-rollback-{skill_name}-") as tmp_str:
        tmp = Path(tmp_str)
        bundle_manifest = unpack_skill_bundle(cached_bundle, tmp)
        verify_bundle_contents(bundle_manifest, tmp)
        bundle_tree_sha = bundle_manifest.tree_sha256
        meta_dir = tmp / SKILL_META_DIR
        if meta_dir.is_dir():
            shutil.rmtree(meta_dir)
        for target in install_targets:
            install_dir = Path(str(target["host_path"])) / skill_name
            install_dir.parent.mkdir(parents=True, exist_ok=True)
            tree_sha = _install_skill_content(tmp, install_dir)
            if install_tree_sha is None:
                install_tree_sha = tree_sha
    return install_tree_sha, bundle_tree_sha


def _write_rollback_lockfile(
    *,
    lockfile_path: Path,
    distributor_id: str,
    skill_name: str,
    target_version: int,
    artifact: Any,
    bundle_tree_sha: str,
    install_tree_sha: str | None,
    reason: str | None,
    emergency_override: bool,
    manifest_version: int,
    changelog: str | None,
) -> None:
    lock_payload = _updated_lockfile_payload(
        lockfile_path=lockfile_path,
        distributor_id=distributor_id,
        skill_name=skill_name,
        entry=SkillLockEntry(
            name=skill_name,
            source="distributor",
            distributor_id=distributor_id,
            version=target_version,
            bundle_sha256=artifact.sha256,
            bundle_tree_sha256=bundle_tree_sha,
            install_tree_sha=install_tree_sha,
            pinned_by="rollback",
            pin_reason=reason,
            extras=_rollback_extras(
                emergency_override=emergency_override,
                manifest_version=manifest_version,
                download_url=artifact.download_url,
                changelog=changelog,
            ),
        ),
    )
    lockfile_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        lockfile_path,
        json.dumps(lock_payload, indent=2, sort_keys=True) + "\n",
    )


def _updated_lockfile_payload(
    *,
    lockfile_path: Path,
    distributor_id: str,
    skill_name: str,
    entry: SkillLockEntry,
) -> dict[str, Any]:
    existing = None
    if Path(lockfile_path).is_file():
        existing = parse_lockfile(json.loads(Path(lockfile_path).read_text(encoding="utf-8")))
    entries = []
    if existing:
        entries = [
            item
            for item in existing.skills
            if not (
                item.source == "distributor"
                and item.distributor_id == distributor_id
                and item.name == skill_name
            )
        ]
    entries.append(entry)
    manifests = existing.distributor_manifests if existing else {}
    return emit_lockfile(
        entries,
        manifests,
        config_sha=existing.config_sha if existing else "rollback",
        synced_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )


def _current_lock_version(
    *,
    lockfile_path: Path,
    distributor_id: str,
    skill_name: str,
) -> int | None:
    if not Path(lockfile_path).is_file():
        return None
    try:
        existing = parse_lockfile(json.loads(Path(lockfile_path).read_text(encoding="utf-8")))
    except Exception:
        return None
    for item in existing.skills:
        if (
            item.source == "distributor"
            and item.distributor_id == distributor_id
            and item.name == skill_name
        ):
            return item.version
    return None


def _rollback_extras(
    *,
    emergency_override: bool,
    manifest_version: int,
    download_url: str,
    changelog: str | None,
) -> dict[str, Any]:
    extras: dict[str, Any] = {
        "rollback_manifest_version": manifest_version,
        "download_url": download_url,
    }
    if emergency_override:
        extras["rollback_emergency_override"] = True
    if changelog:
        extras["changelog"] = changelog
    return extras
