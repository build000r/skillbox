"""Sync pipeline for distributor-set skill sources.

Wires the full fetch → verify → cache → install flow for skills distributed
via signed manifests from a trusted distributor endpoint.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request

from .bundle import SKILL_META_DIR, unpack_skill_bundle, verify_bundle_contents
from .http_security import HttpsOnlyError, require_https, secure_opener
from .lockfile import (
    DistributorManifestLockEntry,
    Lockfile,
    SkillLockEntry,
    emit_lockfile,
    parse_lockfile,
)
from .manifest import parse_manifest, verify_manifest
from .pin_resolver import PinResolutionError, resolve_pin
from .signing import KeyFormatError, SignatureVerificationError, load_public_key

from ..shared import atomic_write_text, host_path_to_absolute_path
from ..shared_distribution import (
    DistributorConfig,
    DistributorSetSource,
    parse_distribution_config,
)


class DistributorSyncError(Exception):
    pass


@dataclass(frozen=True)
class _SelectedSkill:
    version: int
    pinned_by: str
    reason: str | None
    sha256: str
    download_url: str
    changelog: str | None
    targets: list[str]
    capabilities: list[str]


@dataclass(frozen=True)
class _DistributionSyncContext:
    config_path: Path
    lock_path: Path
    state_root: Path
    install_targets: list[dict[str, Any]]
    distributors: dict[str, DistributorConfig]
    sources: list[DistributorSetSource]
    existing_lock: Lockfile | None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

# Hard ceiling for any single fetch. Far larger than any reasonable skill
# manifest or bundle, but bounded so a malicious, compromised, or MITM'd
# endpoint cannot exhaust memory before signature/hash verification runs
# (the verification reads the whole body, so an unbounded read would OOM
# before it could reject bad content).
_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


def _read_capped(resp: Any, url: str, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise DistributorSyncError(
                f"refusing oversized response from {url}: exceeds {max_bytes} byte cap"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _http_get(
    url: str,
    headers: dict[str, str],
    *,
    timeout: float = 30.0,
    max_bytes: int = _MAX_DOWNLOAD_BYTES,
    _opener: Callable | None = None,
) -> bytes:
    try:
        require_https(url)
    except HttpsOnlyError as exc:
        raise DistributorSyncError(str(exc)) from exc
    req = Request(url, headers=headers, method="GET")
    open_fn = _opener or secure_opener().open
    try:
        with open_fn(req, timeout=timeout) as resp:
            return _read_capped(resp, url, max_bytes)
    except HTTPError as exc:
        code = exc.code
        reason = exc.reason
        exc.close()
        raise DistributorSyncError(
            f"HTTP {code} from {url}: {reason}"
        ) from exc
    except (URLError, OSError) as exc:
        raise DistributorSyncError(
            f"network error fetching {url}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# File hashing (mirrors shared.py / bundle.py pattern)
# ---------------------------------------------------------------------------

def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Core sync for one distributor source
# ---------------------------------------------------------------------------

def sync_distributor_set(
    distributor: DistributorConfig,
    source: DistributorSetSource,
    state_root: Path,
    install_targets: list[dict[str, Any]],
    *,
    target_env: str | None = None,
    existing_manifest_version: int | None = None,
    dry_run: bool = False,
    _url_opener: Callable | None = None,
) -> tuple[list[SkillLockEntry], DistributorManifestLockEntry | None]:
    """Sync a single distributor-set source.

    Returns ``(skill_lock_entries, manifest_lock_entry)``.
    ``manifest_lock_entry`` is ``None`` when the manifest version is unchanged
    (idempotency short-circuit).
    """
    auth_headers = _auth_headers(distributor)
    manifest, manifest_bytes = _fetch_verified_manifest(
        distributor,
        auth_headers,
        _url_opener=_url_opener,
    )
    _cache_manifest(state_root, distributor.id, manifest_bytes)

    # -- Idempotency ----------------------------------------------------------
    if (
        existing_manifest_version is not None
        and manifest.manifest_version == existing_manifest_version
    ):
        return [], None

    # -- Process each skill ---------------------------------------------------
    bundle_cache_dir = state_root / "bundle-cache"
    lock_entries: list[SkillLockEntry] = []

    for skill in _selected_manifest_skills(manifest.skills, source, target_env):
        selected = _resolve_selected_skill(skill, source)
        cached_bundle = _ensure_cached_bundle(
            distributor=distributor,
            auth_headers=auth_headers,
            bundle_cache_dir=bundle_cache_dir,
            skill_name=skill.name,
            selected=selected,
            _url_opener=_url_opener,
        )

        if dry_run:
            lock_entries.append(_lock_entry_for_selected(skill.name, distributor.id, selected))
            continue

        bundle_tree_sha, install_tree_sha = _install_cached_bundle(
            cached_bundle=cached_bundle,
            expected_sha256=selected.sha256,
            skill_name=skill.name,
            install_targets=install_targets,
        )
        lock_entries.append(_lock_entry_for_selected(
            skill.name,
            distributor.id,
            selected,
            bundle_tree_sha256=bundle_tree_sha,
            install_tree_sha=install_tree_sha,
        ))

    manifest_lock = DistributorManifestLockEntry(
        manifest_version=manifest.manifest_version,
        fetched_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        signature_verified=True,
    )
    return lock_entries, manifest_lock


def _auth_headers(distributor: DistributorConfig) -> dict[str, str]:
    api_key = os.environ.get(distributor.auth.key_env)
    if not api_key:
        raise DistributorSyncError(
            f"env var '{distributor.auth.key_env}' not set for distributor '{distributor.id}'"
        )
    return {
        "Authorization": f"Bearer {api_key}",
        "X-Client-ID": distributor.client_id,
    }


def _fetch_verified_manifest(
    distributor: DistributorConfig,
    auth_headers: dict[str, str],
    *,
    _url_opener: Callable | None = None,
):
    manifest_url = f"{distributor.url.rstrip('/')}/manifest"
    manifest_bytes = _http_get(
        manifest_url,
        {**auth_headers, "Accept": "application/json"},
        _opener=_url_opener,
    )
    manifest = parse_manifest(manifest_bytes)
    try:
        verify_manifest(manifest, load_public_key(distributor.verification.public_key))
    except (KeyFormatError, SignatureVerificationError) as exc:
        raise DistributorSyncError(
            f"manifest signature verification failed for '{distributor.id}': {exc}"
        ) from exc
    if manifest.distributor_id != distributor.id:
        raise DistributorSyncError(
            f"manifest distributor_id {manifest.distributor_id!r} does not match "
            f"configured distributor id {distributor.id!r}"
        )
    return manifest, manifest_bytes


def _cache_manifest(state_root: Path, distributor_id: str, manifest_bytes: bytes) -> None:
    manifest_cache_dir = state_root / "manifests"
    manifest_cache_dir.mkdir(parents=True, exist_ok=True)
    (manifest_cache_dir / f"{distributor_id}.json").write_bytes(manifest_bytes)


def _selected_manifest_skills(
    skills,
    source: DistributorSetSource,
    target_env: str | None,
):
    selected = list(skills)
    if source.pick:
        pick_set = set(source.pick)
        selected = [skill for skill in selected if skill.name in pick_set]
    if target_env:
        selected = [skill for skill in selected if target_env in skill.targets]
    return selected


def _resolve_selected_skill(skill, source: DistributorSetSource) -> _SelectedSkill:
    client_pin = source.pin.get(skill.name) if source.pin else None
    try:
        resolution = resolve_pin(skill, client_pin)
    except PinResolutionError as exc:
        raise DistributorSyncError(str(exc)) from exc
    artifact = resolution.artifact
    return _SelectedSkill(
        version=resolution.version,
        pinned_by=resolution.pinned_by,
        reason=resolution.reason,
        sha256=artifact.sha256 if artifact else skill.sha256,
        download_url=artifact.download_url if artifact else skill.download_url,
        changelog=artifact.changelog if artifact and artifact.changelog else skill.changelog,
        targets=skill.targets,
        capabilities=skill.capabilities,
    )


def _ensure_cached_bundle(
    *,
    distributor: DistributorConfig,
    auth_headers: dict[str, str],
    bundle_cache_dir: Path,
    skill_name: str,
    selected: _SelectedSkill,
    _url_opener: Callable | None = None,
) -> Path:
    skill_cache = bundle_cache_dir / skill_name
    skill_cache.mkdir(parents=True, exist_ok=True)
    cached_bundle = skill_cache / f"{skill_name}-v{selected.version}.skillbundle.tar.gz"
    if cached_bundle.is_file() and _file_sha256(cached_bundle) == selected.sha256:
        return cached_bundle

    bundle_url = _validated_bundle_url(distributor, skill_name, selected.download_url)
    bundle_bytes = _http_get(
        bundle_url,
        {**auth_headers, "Accept": "application/gzip"},
        _opener=_url_opener,
    )
    cached_bundle.write_bytes(bundle_bytes)
    actual_hash = _file_sha256(cached_bundle)
    if actual_hash != selected.sha256:
        cached_bundle.unlink(missing_ok=True)
        raise DistributorSyncError(
            f"bundle SHA256 mismatch for {skill_name}: "
            f"expected {selected.sha256}, got {actual_hash}"
        )
    return cached_bundle


def _validated_bundle_url(
    distributor: DistributorConfig,
    skill_name: str,
    download_url: str,
) -> str:
    bundle_url = f"{distributor.url.rstrip('/')}{download_url}"
    distributor_parsed = urlparse(distributor.url)
    bundle_parsed = urlparse(bundle_url)
    if (
        bundle_parsed.scheme == distributor_parsed.scheme
        and (bundle_parsed.hostname or "").lower()
            == (distributor_parsed.hostname or "").lower()
        and bundle_parsed.port == distributor_parsed.port
    ):
        return bundle_url
    raise DistributorSyncError(
        f"refusing to fetch bundle for {skill_name!r}: "
        f"download_url resolves to a different host than the "
        f"configured distributor ({bundle_parsed.hostname!r} != "
        f"{distributor_parsed.hostname!r})"
    )


def _install_cached_bundle(
    *,
    cached_bundle: Path,
    expected_sha256: str,
    skill_name: str,
    install_targets: list[dict[str, Any]],
) -> tuple[str, str | None]:
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        with cached_bundle.open("rb") as bundle_fh:
            hasher = hashlib.sha256()
            for chunk in iter(lambda: bundle_fh.read(65536), b""):
                hasher.update(chunk)
            pre_unpack_hash = hasher.hexdigest()
            if pre_unpack_hash != expected_sha256:
                raise DistributorSyncError(
                    f"bundle SHA256 mismatch for {skill_name} prior to unpack: "
                    f"expected {expected_sha256}, got {pre_unpack_hash}"
                )
            bundle_fh.seek(0)
            bundle_manifest = unpack_skill_bundle(bundle_fh, tmp)
        verify_bundle_contents(bundle_manifest, tmp)
        _remove_bundle_metadata(tmp)
        install_tree_sha = _install_to_targets(tmp, skill_name, install_targets)
        return bundle_manifest.tree_sha256, install_tree_sha


def _remove_bundle_metadata(tmp: Path) -> None:
    meta_dir = tmp / SKILL_META_DIR
    if meta_dir.is_dir():
        shutil.rmtree(meta_dir)


def _install_to_targets(
    tmp: Path,
    skill_name: str,
    install_targets: list[dict[str, Any]],
) -> str | None:
    install_tree_sha: str | None = None
    for target in install_targets:
        target_root = Path(str(target["host_path"]))
        install_dir = target_root / skill_name
        install_dir.parent.mkdir(parents=True, exist_ok=True)
        tree_sha = _install_skill_content(tmp, install_dir)
        if install_tree_sha is None:
            install_tree_sha = tree_sha
    return install_tree_sha


def _lock_entry_for_selected(
    skill_name: str,
    distributor_id: str,
    selected: _SelectedSkill,
    *,
    bundle_tree_sha256: str | None = None,
    install_tree_sha: str | None = None,
) -> SkillLockEntry:
    return SkillLockEntry(
        name=skill_name,
        source="distributor",
        distributor_id=distributor_id,
        version=selected.version,
        bundle_sha256=selected.sha256,
        bundle_tree_sha256=bundle_tree_sha256,
        install_tree_sha=install_tree_sha,
        pinned_by=selected.pinned_by,
        pin_reason=selected.reason,
        extras=_selected_artifact_extras(
            download_url=selected.download_url,
            targets=selected.targets,
            capabilities=selected.capabilities,
            changelog=selected.changelog,
        ),
    )


def _install_skill_content(source_dir: Path, target_dir: Path) -> str:
    from ..shared import filtered_copy_skill
    return filtered_copy_skill(source_dir, target_dir)


def _selected_artifact_extras(
    *,
    download_url: str,
    targets: list[str],
    capabilities: list[str],
    changelog: str | None,
) -> dict[str, Any]:
    extras: dict[str, Any] = {
        "download_url": download_url,
        "manifest_targets": list(targets),
    }
    if capabilities:
        extras["capabilities"] = list(capabilities)
    if changelog:
        extras["changelog"] = changelog
    return extras


# ---------------------------------------------------------------------------
# Model-level orchestrator (called from sync_runtime)
# ---------------------------------------------------------------------------

def sync_distributor_sources(model: dict[str, Any], dry_run: bool) -> list[str]:
    """Sync all distributor-set sources across skill-repo-set skillsets.

    Called from ``runtime_ops.sync_runtime`` after ``sync_skill_repo_sets``
    has handled repo/path sources.  Reads the existing lockfile (written by
    the repo-set sync), merges distributor entries, and writes back.
    """
    actions: list[str] = []
    for context in _distribution_sync_contexts(model):
        actions.extend(_sync_distribution_context(context, dry_run=dry_run))
    return actions


def _distribution_sync_contexts(model: dict[str, Any]) -> list[_DistributionSyncContext]:
    contexts: list[_DistributionSyncContext] = []
    root_dir = Path(str(model.get("root_dir", "")))
    state_root = _state_root_for_model(model, root_dir)
    for skillset in model.get("skills") or []:
        if not _supports_distributor_sync(skillset):
            continue
        context = _distribution_sync_context(skillset, state_root)
        if context is not None:
            contexts.append(context)
    return contexts


def _supports_distributor_sync(skillset: Any) -> bool:
    if not isinstance(skillset, dict) or skillset.get("kind") != "skill-repo-set":
        return False
    return (skillset.get("sync") or {}).get("mode", "") == "clone-and-install"


def _state_root_for_model(model: dict[str, Any], root_dir: Path) -> Path:
    storage = model.get("storage") if isinstance(model.get("storage"), dict) else {}
    env_values = model.get("env") if isinstance(model.get("env"), dict) else {}
    raw_state_root = str(
        (storage or {}).get("state_root")
        or (env_values or {}).get("SKILLBOX_STATE_ROOT")
        or ".skillbox-state"
    ).strip()
    return host_path_to_absolute_path(root_dir, raw_state_root)


def _distribution_sync_context(
    skillset: dict[str, Any],
    state_root: Path,
) -> _DistributionSyncContext | None:
    from ..shared import load_skill_repos_config

    config_path = Path(str(skillset.get("skill_repos_config_host_path", "")))
    lock_path = Path(str(skillset.get("lock_path_host_path", "")))
    try:
        config = load_skill_repos_config(config_path)
    except Exception:
        return None
    distributors, sources = parse_distribution_config(config, config_path)
    if not sources:
        return None
    return _DistributionSyncContext(
        config_path=config_path,
        lock_path=lock_path,
        state_root=state_root,
        install_targets=skillset.get("install_targets") or [],
        distributors=distributors,
        sources=sources,
        existing_lock=_read_existing_lockfile(lock_path),
    )


def _read_existing_lockfile(lock_path: Path) -> Lockfile | None:
    if not lock_path.is_file():
        return None
    try:
        raw_lock = json.loads(lock_path.read_text(encoding="utf-8"))
        return parse_lockfile(raw_lock)
    except Exception:
        return None


def _sync_distribution_context(
    context: _DistributionSyncContext,
    *,
    dry_run: bool,
) -> list[str]:
    actions: list[str] = []
    all_dist_entries: list[SkillLockEntry] = []
    all_manifest_entries: dict[str, DistributorManifestLockEntry] = {}

    for source in context.sources:
        _sync_distribution_source(
            context,
            source,
            dry_run=dry_run,
            actions=actions,
            entries_out=all_dist_entries,
            manifests_out=all_manifest_entries,
        )

    if not all_dist_entries and not all_manifest_entries:
        return actions

    lock_payload = _merged_lock_payload(
        context,
        all_dist_entries,
        all_manifest_entries,
    )
    if not dry_run:
        context.lock_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            context.lock_path,
            json.dumps(lock_payload, indent=2, sort_keys=True) + "\n",
        )
    actions.append(f"write-lockfile: {context.lock_path} (distribution-merged)")
    return actions


def _sync_distribution_source(
    context: _DistributionSyncContext,
    source: DistributorSetSource,
    *,
    dry_run: bool,
    actions: list[str],
    entries_out: list[SkillLockEntry],
    manifests_out: dict[str, DistributorManifestLockEntry],
) -> None:
    try:
        entries, manifest_entry = sync_distributor_set(
            context.distributors[source.distributor],
            source,
            context.state_root,
            context.install_targets,
            existing_manifest_version=_existing_manifest_version(context.existing_lock, source),
            dry_run=dry_run,
        )
    except DistributorSyncError as exc:
        actions.append(f"distributor-sync-error: {source.distributor}: {exc}")
        _carry_forward(context.existing_lock, source.distributor, entries_out, manifests_out)
        return

    if manifest_entry is None:
        actions.append(f"distributor-unchanged: {source.distributor}")
        _carry_forward(context.existing_lock, source.distributor, entries_out, manifests_out)
        return

    entries_out.extend(entries)
    manifests_out[source.distributor] = manifest_entry
    for entry in entries:
        actions.append(
            f"install-distributor-skill: {entry.name} (v{entry.version}) "
            f"from {source.distributor}"
        )


def _existing_manifest_version(
    existing_lock: Lockfile | None,
    source: DistributorSetSource,
) -> int | None:
    if not existing_lock:
        return None
    manifest_entry = existing_lock.distributor_manifests.get(source.distributor)
    return manifest_entry.manifest_version if manifest_entry else None


def _merged_lock_payload(
    context: _DistributionSyncContext,
    dist_entries: list[SkillLockEntry],
    manifest_entries: dict[str, DistributorManifestLockEntry],
) -> dict[str, Any]:
    from ..shared import file_sha256 as shared_file_sha256

    existing_lock = context.existing_lock
    non_dist: list[SkillLockEntry] = []
    if existing_lock:
        non_dist = [entry for entry in existing_lock.skills if entry.source != "distributor"]
        for distributor_id, manifest_entry in existing_lock.distributor_manifests.items():
            manifest_entries.setdefault(distributor_id, manifest_entry)
    config_sha = (
        existing_lock.config_sha if existing_lock
        else (shared_file_sha256(context.config_path) if context.config_path.is_file() else "")
    )
    return emit_lockfile(
        non_dist + dist_entries,
        manifest_entries,
        config_sha=config_sha,
        synced_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )


def _carry_forward(
    existing_lock: Lockfile | None,
    distributor_id: str,
    entries_out: list[SkillLockEntry],
    manifests_out: dict[str, DistributorManifestLockEntry],
) -> None:
    """Preserve existing distributor state when sync short-circuits or errors."""
    if not existing_lock:
        return
    for entry in existing_lock.skills:
        if entry.source == "distributor" and entry.distributor_id == distributor_id:
            entries_out.append(entry)
    me = existing_lock.distributor_manifests.get(distributor_id)
    if me:
        manifests_out[distributor_id] = me
