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
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .bundle import SKILL_META_DIR, unpack_skill_bundle, verify_bundle_contents
from .lockfile import (
    DistributorManifestLockEntry,
    Lockfile,
    SkillLockEntry,
    emit_lockfile,
    parse_lockfile,
)
from .manifest import parse_manifest, verify_manifest
from .pin_resolver import resolve_pin
from .signing import SignatureVerificationError, load_public_key

from ..shared_distribution import (
    DistributorConfig,
    DistributorSetSource,
    parse_distribution_config,
)


class DistributorSyncError(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _http_get(
    url: str,
    headers: dict[str, str],
    *,
    timeout: float = 30.0,
    _opener: Callable | None = None,
) -> bytes:
    req = Request(url, headers=headers, method="GET")
    open_fn = _opener or urlopen
    try:
        resp = open_fn(req, timeout=timeout)
        return resp.read()
    except HTTPError as exc:
        raise DistributorSyncError(
            f"HTTP {exc.code} from {url}: {exc.reason}"
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
    api_key = os.environ.get(distributor.auth.key_env)
    if not api_key:
        raise DistributorSyncError(
            f"env var '{distributor.auth.key_env}' not set for distributor '{distributor.id}'"
        )

    auth_headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Client-ID": distributor.client_id,
    }

    # -- Fetch manifest -------------------------------------------------------
    manifest_url = f"{distributor.url.rstrip('/')}/manifest"
    manifest_bytes = _http_get(
        manifest_url,
        {**auth_headers, "Accept": "application/json"},
        _opener=_url_opener,
    )

    manifest = parse_manifest(manifest_bytes)
    public_key = load_public_key(distributor.verification.public_key)
    try:
        verify_manifest(manifest, public_key)
    except SignatureVerificationError as exc:
        raise DistributorSyncError(
            f"manifest signature verification failed for '{distributor.id}': {exc}"
        ) from exc

    # -- Cache raw manifest ---------------------------------------------------
    manifest_cache_dir = state_root / "manifests"
    manifest_cache_dir.mkdir(parents=True, exist_ok=True)
    (manifest_cache_dir / f"{distributor.id}.json").write_bytes(manifest_bytes)

    # -- Idempotency ----------------------------------------------------------
    if (
        existing_manifest_version is not None
        and manifest.manifest_version == existing_manifest_version
    ):
        return [], None

    # -- Filter skills --------------------------------------------------------
    skills = list(manifest.skills)
    if source.pick:
        pick_set = set(source.pick)
        skills = [s for s in skills if s.name in pick_set]
    if target_env:
        skills = [s for s in skills if target_env in s.targets]

    # -- Process each skill ---------------------------------------------------
    bundle_cache_dir = state_root / "bundle-cache"
    lock_entries: list[SkillLockEntry] = []

    for skill in skills:
        client_pin = source.pin.get(skill.name) if source.pin else None
        resolution = resolve_pin(skill, client_pin)

        skill_cache = bundle_cache_dir / skill.name
        skill_cache.mkdir(parents=True, exist_ok=True)
        bundle_filename = f"{skill.name}-v{resolution.version}.skillbundle.tar.gz"
        cached_bundle = skill_cache / bundle_filename

        need_download = True
        if cached_bundle.is_file() and _file_sha256(cached_bundle) == skill.sha256:
            need_download = False

        if need_download:
            bundle_url = f"{distributor.url.rstrip('/')}{skill.download_url}"
            bundle_bytes = _http_get(
                bundle_url,
                {**auth_headers, "Accept": "application/gzip"},
                _opener=_url_opener,
            )
            cached_bundle.write_bytes(bundle_bytes)

            actual_hash = _file_sha256(cached_bundle)
            if actual_hash != skill.sha256:
                cached_bundle.unlink(missing_ok=True)
                raise DistributorSyncError(
                    f"bundle SHA256 mismatch for {skill.name}: "
                    f"expected {skill.sha256}, got {actual_hash}"
                )

        if dry_run:
            lock_entries.append(SkillLockEntry(
                name=skill.name,
                source="distributor",
                distributor_id=distributor.id,
                version=resolution.version,
                bundle_sha256=skill.sha256,
                pinned_by=resolution.pinned_by,
                pin_reason=resolution.reason,
            ))
            continue

        # Unpack, verify, install
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bundle_manifest = unpack_skill_bundle(cached_bundle, tmp)
            verify_bundle_contents(bundle_manifest, tmp)

            meta_dir = tmp / SKILL_META_DIR
            if meta_dir.is_dir():
                shutil.rmtree(meta_dir)

            install_tree_sha: str | None = None
            for target in install_targets:
                target_root = Path(str(target["host_path"]))
                install_dir = target_root / skill.name
                install_dir.parent.mkdir(parents=True, exist_ok=True)
                tree_sha = _install_skill_content(tmp, install_dir)
                if install_tree_sha is None:
                    install_tree_sha = tree_sha

        lock_entries.append(SkillLockEntry(
            name=skill.name,
            source="distributor",
            distributor_id=distributor.id,
            version=resolution.version,
            bundle_sha256=skill.sha256,
            bundle_tree_sha256=bundle_manifest.tree_sha256,
            install_tree_sha=install_tree_sha,
            pinned_by=resolution.pinned_by,
            pin_reason=resolution.reason,
        ))

    manifest_lock = DistributorManifestLockEntry(
        manifest_version=manifest.manifest_version,
        fetched_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        signature_verified=True,
    )
    return lock_entries, manifest_lock


def _install_skill_content(source_dir: Path, target_dir: Path) -> str:
    from ..shared import filtered_copy_skill
    return filtered_copy_skill(source_dir, target_dir)


# ---------------------------------------------------------------------------
# Model-level orchestrator (called from sync_runtime)
# ---------------------------------------------------------------------------

def sync_distributor_sources(model: dict[str, Any], dry_run: bool) -> list[str]:
    """Sync all distributor-set sources across skill-repo-set skillsets.

    Called from ``runtime_ops.sync_runtime`` after ``sync_skill_repo_sets``
    has handled repo/path sources.  Reads the existing lockfile (written by
    the repo-set sync), merges distributor entries, and writes back.
    """
    from ..shared import file_sha256 as shared_file_sha256, load_skill_repos_config

    actions: list[str] = []

    for skillset in model.get("skills") or []:
        if skillset.get("kind") != "skill-repo-set":
            continue
        sync_mode = (skillset.get("sync") or {}).get("mode", "")
        if sync_mode != "clone-and-install":
            continue

        config_path = Path(str(skillset.get("skill_repos_config_host_path", "")))
        lock_path = Path(str(skillset.get("lock_path_host_path", "")))
        root_dir = Path(str(model.get("root_dir", "")))
        state_root = root_dir / ".skillbox-state"

        try:
            config = load_skill_repos_config(config_path)
        except Exception:
            continue
        distributors, sources = parse_distribution_config(config, config_path)
        if not sources:
            continue

        install_targets = skillset.get("install_targets") or []

        # Read existing lockfile (may be v2 from repo-set sync or v3 from prior run)
        existing_lock: Lockfile | None = None
        if lock_path.is_file():
            try:
                raw_lock = json.loads(lock_path.read_text(encoding="utf-8"))
                existing_lock = parse_lockfile(raw_lock)
            except Exception:
                pass

        all_dist_entries: list[SkillLockEntry] = []
        all_manifest_entries: dict[str, DistributorManifestLockEntry] = {}

        for source in sources:
            dist_config = distributors[source.distributor]

            existing_mv: int | None = None
            if existing_lock:
                me = existing_lock.distributor_manifests.get(source.distributor)
                if me:
                    existing_mv = me.manifest_version

            try:
                entries, manifest_entry = sync_distributor_set(
                    dist_config,
                    source,
                    state_root,
                    install_targets,
                    existing_manifest_version=existing_mv,
                    dry_run=dry_run,
                )
            except DistributorSyncError as exc:
                actions.append(
                    f"distributor-sync-error: {source.distributor}: {exc}"
                )
                _carry_forward(existing_lock, source.distributor,
                               all_dist_entries, all_manifest_entries)
                continue

            if manifest_entry is None:
                actions.append(f"distributor-unchanged: {source.distributor}")
                _carry_forward(existing_lock, source.distributor,
                               all_dist_entries, all_manifest_entries)
                continue

            all_dist_entries.extend(entries)
            all_manifest_entries[source.distributor] = manifest_entry
            for e in entries:
                actions.append(
                    f"install-distributor-skill: {e.name} (v{e.version}) "
                    f"from {source.distributor}"
                )

        if not all_dist_entries and not all_manifest_entries:
            continue

        # Merge with existing non-distributor entries
        non_dist: list[SkillLockEntry] = []
        if existing_lock:
            non_dist = [e for e in existing_lock.skills if e.source != "distributor"]
            for did, me in existing_lock.distributor_manifests.items():
                all_manifest_entries.setdefault(did, me)

        config_sha = (existing_lock.config_sha if existing_lock
                       else (shared_file_sha256(config_path)
                             if config_path.is_file() else ""))
        synced_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        lock_payload = emit_lockfile(
            non_dist + all_dist_entries,
            all_manifest_entries,
            config_sha=config_sha,
            synced_at=synced_at,
        )

        if not dry_run:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps(lock_payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        actions.append(f"write-lockfile: {lock_path} (distribution-merged)")

    return actions


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
