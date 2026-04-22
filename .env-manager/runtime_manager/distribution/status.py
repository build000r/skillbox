"""Status and context rendering helpers for distributor integrations."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..shared import load_skill_repos_config
from ..shared_distribution import parse_distribution_config
from .lockfile import Lockfile, parse_lockfile

AUTH_PROBE_VALUES = {"ok", "failed", "offline", "unknown"}


def collect_distributor_status(model: dict[str, Any]) -> list[dict[str, Any]]:
    """Return distributor status rows for runtime-status and context rendering.

    This surface is intentionally observational:
    - no network probes
    - auth probe uses cached doctor output when present
    - falls back to ``unknown`` when no cache exists
    """
    root_dir = Path(str(model.get("root_dir") or "")).resolve()
    state_root = root_dir / ".skillbox-state"

    rows_by_id: dict[str, dict[str, Any]] = {}
    for skillset in model.get("skills") or []:
        if skillset.get("kind") != "skill-repo-set":
            continue

        config_path = Path(str(skillset.get("skill_repos_config_host_path") or "")).resolve()
        lock_path = Path(str(skillset.get("lock_path_host_path") or "")).resolve()

        config = _load_distribution_config(config_path)
        if not config:
            continue
        distributors = config["distributors"]
        lock = _read_lockfile(lock_path)

        for distributor_id, distributor in distributors.items():
            row = rows_by_id.setdefault(
                distributor_id,
                {
                    "id": distributor_id,
                    "client_id": distributor.client_id,
                    "url": distributor.url,
                    "skills_count": 0,
                    "manifest_version": None,
                    "last_sync": None,
                    "auth_key_present": False,
                    "auth_probe_result": "unknown",
                },
            )

            row["auth_key_present"] = bool(os.environ.get(distributor.auth.key_env))
            row["auth_probe_result"] = _merge_auth_probe(
                row["auth_probe_result"],
                _read_cached_auth_probe_result(state_root, distributor_id),
            )

            if lock is not None:
                row["skills_count"] += _count_distributor_skills(lock, distributor_id)
                manifest_entry = lock.distributor_manifests.get(distributor_id)
                if manifest_entry is not None:
                    row["manifest_version"] = _max_optional_int(
                        row["manifest_version"],
                        manifest_entry.manifest_version,
                    )
                    row["last_sync"] = _latest_timestamp(
                        row["last_sync"],
                        manifest_entry.fetched_at,
                    )
                row["last_sync"] = _latest_timestamp(row["last_sync"], lock.synced_at)

            cached_manifest = _read_cached_manifest(state_root, distributor_id)
            if cached_manifest:
                cached_manifest_version = cached_manifest.get("manifest_version")
                if isinstance(cached_manifest_version, int) and not isinstance(cached_manifest_version, bool):
                    row["manifest_version"] = _max_optional_int(
                        row["manifest_version"],
                        cached_manifest_version,
                    )
                if row["skills_count"] == 0:
                    cached_skills = cached_manifest.get("skills")
                    if isinstance(cached_skills, list):
                        row["skills_count"] = len(cached_skills)
                row["last_sync"] = _latest_timestamp(
                    row["last_sync"],
                    _optional_non_empty_str(cached_manifest.get("updated_at")),
                )

    return [rows_by_id[key] for key in sorted(rows_by_id)]


def render_connected_distributors_section(model: dict[str, Any]) -> list[str]:
    """Render markdown lines for the Connected Distributors context section."""
    distributors = collect_distributor_status(model)
    if not distributors:
        return []

    lines = [
        "## Connected Distributors",
        "",
        "| ID | Client | URL | Skills | Manifest | Last Sync | Auth Key | Auth Probe |",
        "|----|--------|-----|--------|----------|-----------|----------|------------|",
    ]
    for row in distributors:
        manifest_version = str(row["manifest_version"]) if row["manifest_version"] is not None else "-"
        last_sync = row["last_sync"] or "-"
        auth_key = "yes" if row["auth_key_present"] else "no"
        lines.append(
            f"| {row['id']} | {row['client_id']} | `{row['url']}` | "
            f"{row['skills_count']} | {manifest_version} | {last_sync} | "
            f"{auth_key} | {row['auth_probe_result']} |"
        )
    lines.append("")
    return lines


def _load_distribution_config(config_path: Path) -> dict[str, Any] | None:
    if not config_path.is_file():
        return None
    try:
        raw = load_skill_repos_config(config_path)
        if "distributors" not in raw:
            return None
        distributors, sources = parse_distribution_config(raw, config_path)
        return {"distributors": distributors, "sources": sources}
    except Exception:
        return None


def _read_lockfile(lock_path: Path) -> Lockfile | None:
    if not lock_path.is_file():
        return None
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    try:
        return parse_lockfile(payload)
    except Exception:
        return None


def _read_cached_manifest(state_root: Path, distributor_id: str) -> dict[str, Any] | None:
    manifest_path = state_root / "manifests" / f"{distributor_id}.json"
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _read_cached_auth_probe_result(state_root: Path, distributor_id: str) -> str:
    manifests_dir = state_root / "manifests"
    candidates = [
        manifests_dir / f"{distributor_id}.auth-probe.json",
        manifests_dir / f"{distributor_id}.probe.json",
        manifests_dir / f"{distributor_id}.doctor.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        raw_value = (
            payload.get("auth_probe_result")
            or payload.get("auth_probe")
            or payload.get("probe_result")
            or payload.get("status")
        )
        normalized = _normalize_auth_probe(raw_value)
        if normalized != "unknown":
            return normalized
    return "unknown"


def _normalize_auth_probe(raw_value: Any) -> str:
    value = str(raw_value or "").strip().lower()
    if value in AUTH_PROBE_VALUES:
        return value
    if value in {"pass", "success"}:
        return "ok"
    if value in {"fail", "error"}:
        return "failed"
    if value in {"timeout", "unreachable"}:
        return "offline"
    return "unknown"


def _merge_auth_probe(current: str, candidate: str) -> str:
    normalized_current = _normalize_auth_probe(current)
    normalized_candidate = _normalize_auth_probe(candidate)
    if normalized_current == "unknown":
        return normalized_candidate
    return normalized_current


def _count_distributor_skills(lock: Lockfile, distributor_id: str) -> int:
    count = 0
    for entry in lock.skills:
        if entry.source == "distributor" and entry.distributor_id == distributor_id:
            count += 1
    return count


def _max_optional_int(current: int | None, candidate: int | None) -> int | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    return max(current, candidate)


def _latest_timestamp(current: str | None, candidate: str | None) -> str | None:
    if not candidate:
        return current
    if not current:
        return candidate
    return candidate if candidate > current else current


def _optional_non_empty_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
