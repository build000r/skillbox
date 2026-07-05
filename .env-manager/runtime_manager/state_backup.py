from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .errors import SkillboxError

try:
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised only without PyYAML.
    yaml = None  # type: ignore


STATE_BACKUP_SCHEMA_VERSION = "2026-07-04+state-backup.v1"
STATE_ROOT_ENV = "SKILLBOX_STATE_ROOT"
BACKUP_ROOT_ENV = "SKILLBOX_BACKUP_ROOT"
DEFAULT_EXCLUDES = ("monoserver/", "__pycache__", "pruned-skill-repo-extras-*")
DRILL_EVIDENCE_REL = Path("state-backup") / "last-drill.json"
PULSE_PID_RELS = (
    Path("logs") / "runtime" / "pulse.pid",
    Path("pulse.pid"),
)


class StateBackupError(SkillboxError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        context: dict[str, Any] | None = None,
        next_actions: Iterable[str] = (),
        recoverable: bool = True,
    ) -> None:
        super().__init__(
            code,
            message,
            context=context,
            next_actions=next_actions,
            recoverable=recoverable,
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _expand_path(raw: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()


def _is_under_or_equal(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_state_root(
    state_root: str | os.PathLike[str] | None = None,
    *,
    model: dict[str, Any] | None = None,
) -> Path:
    raw = str(state_root or os.environ.get(STATE_ROOT_ENV) or "").strip()
    if not raw and isinstance(model, dict):
        raw = str((model.get("storage") or {}).get("state_root") or "").strip()
    if not raw:
        raise StateBackupError(
            "STATE_BACKUP_STATE_ROOT_MISSING",
            f"{STATE_ROOT_ENV} is required for state-backup.",
        )
    resolved = _expand_path(raw)
    if not resolved.is_dir():
        raise StateBackupError(
            "STATE_BACKUP_STATE_ROOT_MISSING",
            f"State root does not exist or is not a directory: {resolved}",
            context={"state_root": str(resolved)},
        )
    return resolved


def _resolve_backup_root(backup_root: str | os.PathLike[str] | None = None) -> Path:
    raw = str(backup_root or os.environ.get(BACKUP_ROOT_ENV) or "").strip()
    if not raw:
        raise StateBackupError(
            "STATE_BACKUP_ROOT_MISSING",
            f"{BACKUP_ROOT_ENV} is required for state-backup.",
        )
    return _expand_path(raw)


def _validate_backup_root(state_root: Path, backup_root: Path) -> None:
    if _is_under_or_equal(backup_root, state_root):
        raise StateBackupError(
            "STATE_BACKUP_DEST_INSIDE_SOURCE",
            "SKILLBOX_BACKUP_ROOT must be outside SKILLBOX_STATE_ROOT.",
            context={"state_root": str(state_root), "backup_root": str(backup_root)},
        )


def _excluded(relpath: Path, is_dir: bool, excludes: tuple[str, ...]) -> str | None:
    rel_posix = relpath.as_posix()
    name = relpath.name
    parts = relpath.parts
    for pattern in excludes:
        if pattern.endswith("/"):
            directory_name = pattern.rstrip("/")
            if is_dir and name == directory_name:
                return pattern
            if directory_name in parts:
                return pattern
            continue
        if name == pattern or fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel_posix, pattern):
            return pattern
        if pattern in parts:
            return pattern
    return None


def _scan_state_root(state_root: Path, excludes: tuple[str, ...]) -> tuple[list[Path], dict[str, Any]]:
    included: list[Path] = []
    file_count = 0
    total_bytes = 0
    excluded_patterns: set[str] = set()
    top_level_entries: set[str] = set()

    for current_root, dir_names, file_names in os.walk(state_root, topdown=True, followlinks=False):
        current = Path(current_root)
        rel_current = current.relative_to(state_root)

        kept_dirs: list[str] = []
        for name in sorted(dir_names):
            path = current / name
            relpath = path.relative_to(state_root)
            matched = _excluded(relpath, True, excludes)
            if matched:
                excluded_patterns.add(matched)
                continue
            kept_dirs.append(name)
            included.append(path)
            if relpath.parts:
                top_level_entries.add(relpath.parts[0])
        dir_names[:] = kept_dirs

        for name in sorted(file_names):
            path = current / name
            relpath = path.relative_to(state_root)
            matched = _excluded(relpath, False, excludes)
            if matched:
                excluded_patterns.add(matched)
                continue
            included.append(path)
            if relpath.parts:
                top_level_entries.add(relpath.parts[0])
            try:
                stat = path.stat() if not path.is_symlink() else path.lstat()
            except OSError:
                continue
            if path.is_file() and not path.is_symlink():
                file_count += 1
                total_bytes += int(stat.st_size)

    summary = {
        "file_count": file_count,
        "total_bytes": total_bytes,
        "excludes_applied": [pattern for pattern in excludes if pattern in excluded_patterns],
        "top_level_entries": sorted(top_level_entries),
    }
    return included, summary


def _ensure_free_space(backup_root: Path, required_bytes: int) -> None:
    free_bytes = int(shutil.disk_usage(backup_root).free)
    required = max(int(required_bytes), 1)
    if free_bytes < required:
        raise StateBackupError(
            "STATE_BACKUP_INSUFFICIENT_SPACE",
            "Not enough free space in SKILLBOX_BACKUP_ROOT for state backup.",
            context={
                "backup_root": str(backup_root),
                "free_bytes": free_bytes,
                "required_bytes": required,
            },
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_backup_paths(backup_root: Path, created_at: datetime) -> tuple[Path, Path]:
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    for index in range(0, 1000):
        suffix = "" if index == 0 else f"-{index}"
        base = f"skillbox-state-{stamp}{suffix}"
        archive = backup_root / f"{base}.tar.gz"
        manifest = backup_root / f"{base}.manifest.json"
        if not archive.exists() and not manifest.exists():
            return archive, manifest
    raise StateBackupError("STATE_BACKUP_NAME_EXHAUSTED", "Could not allocate a unique backup filename.")


def _write_json_0600(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(path, 0o600)


def _drill_evidence_path(state_root: Path) -> Path:
    return state_root / DRILL_EVIDENCE_REL


def create_state_backup(
    *,
    state_root: str | os.PathLike[str] | None = None,
    backup_root: str | os.PathLike[str] | None = None,
    model: dict[str, Any] | None = None,
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
) -> dict[str, Any]:
    source_root = _resolve_state_root(state_root, model=model)
    destination_root = _resolve_backup_root(backup_root)
    _validate_backup_root(source_root, destination_root)
    destination_root.mkdir(parents=True, exist_ok=True)

    included_paths, summary = _scan_state_root(source_root, excludes)
    _ensure_free_space(destination_root, int(summary["total_bytes"]))

    created_at = _utc_now()
    archive_path, manifest_path = _unique_backup_paths(destination_root, created_at)
    try:
        with tarfile.open(archive_path, "w:gz") as archive:
            for path in included_paths:
                relpath = path.relative_to(source_root).as_posix()
                archive.add(path, arcname=relpath, recursive=False)
        os.chmod(archive_path, 0o600)

        sha256 = _sha256_file(archive_path)
        manifest = {
            "schema_version": STATE_BACKUP_SCHEMA_VERSION,
            "created_at": _isoformat(created_at),
            "source_root": str(source_root),
            "archive": str(archive_path),
            "archive_name": archive_path.name,
            "manifest": str(manifest_path),
            "file_count": summary["file_count"],
            "total_bytes": summary["total_bytes"],
            "archive_bytes": archive_path.stat().st_size,
            "sha256": sha256,
            "excludes_applied": summary["excludes_applied"],
            "top_level_entries": summary["top_level_entries"],
        }
        _write_json_0600(manifest_path, manifest)
    except Exception:
        for path in (archive_path, manifest_path):
            try:
                path.unlink()
            except OSError:
                pass
        raise

    return {
        "ok": True,
        "action": "create",
        "backup": manifest,
        "next_actions": [
            f"state-backup verify {manifest_path} --format json",
            "state-backup list --format json",
        ],
    }


def _manifest_path_for(target: str | os.PathLike[str]) -> Path:
    path = _expand_path(target)
    if path.name.endswith(".manifest.json"):
        return path
    if path.name.endswith(".tar.gz"):
        candidate = path.with_name(path.name[:-7] + ".manifest.json")
        if candidate.is_file():
            return candidate
        legacy = path.with_suffix(path.suffix + ".manifest.json")
        if legacy.is_file():
            return legacy
    return path


def _load_manifest(target: str | os.PathLike[str]) -> tuple[Path, dict[str, Any]]:
    manifest_path = _manifest_path_for(target)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StateBackupError(
            "STATE_BACKUP_MANIFEST_NOT_FOUND",
            f"Backup manifest not found: {manifest_path}",
            context={"manifest": str(manifest_path)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise StateBackupError(
            "STATE_BACKUP_MANIFEST_INVALID",
            f"Backup manifest is not valid JSON: {manifest_path}",
            context={"manifest": str(manifest_path)},
        ) from exc
    if not isinstance(manifest, dict):
        raise StateBackupError(
            "STATE_BACKUP_MANIFEST_INVALID",
            f"Backup manifest must be an object: {manifest_path}",
            context={"manifest": str(manifest_path)},
        )
    return manifest_path, manifest


def _archive_path_from_manifest(manifest_path: Path, manifest: dict[str, Any]) -> Path:
    archive_name = str(manifest.get("archive_name") or "").strip()
    if archive_name:
        sibling = (manifest_path.parent / archive_name).resolve()
        if sibling.is_file():
            return sibling
    raw = str(manifest.get("archive") or "").strip()
    if raw:
        archive = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not archive.is_absolute():
            archive = manifest_path.parent / archive
        return archive.resolve()
    if archive_name:
        return (manifest_path.parent / archive_name).resolve()
    if manifest_path.name.endswith(".manifest.json"):
        return manifest_path.with_name(manifest_path.name[:-14] + ".tar.gz").resolve()
    raise StateBackupError(
        "STATE_BACKUP_MANIFEST_INVALID",
        f"Backup manifest does not identify an archive: {manifest_path}",
        context={"manifest": str(manifest_path)},
    )


def _validate_tar_member_path(member: tarfile.TarInfo, destination: Path) -> None:
    name = str(member.name or "")
    parts = PurePosixPath(name).parts
    if not name or name.startswith("/") or ".." in parts:
        raise StateBackupError(
            "STATE_BACKUP_TAR_PATH_ESCAPE",
            f"Backup archive member escapes restore root: {name!r}",
            context={"member": name},
        )
    target = (destination / PurePosixPath(name)).resolve()
    if not _is_under_or_equal(target, destination):
        raise StateBackupError(
            "STATE_BACKUP_TAR_PATH_ESCAPE",
            f"Backup archive member escapes restore root: {name!r}",
            context={"member": name, "target": str(target), "destination": str(destination)},
        )
    if member.issym() or member.islnk():
        link_name = str(member.linkname or "")
        link_parts = PurePosixPath(link_name).parts
        if not link_name or link_name.startswith("/") or ".." in link_parts:
            raise StateBackupError(
                "STATE_BACKUP_TAR_LINK_ESCAPE",
                f"Backup archive link escapes restore root: {name!r} -> {link_name!r}",
                context={"member": name, "linkname": link_name},
            )


def _safe_tar_filter(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo | None:
    dest_path = Path(destination).resolve()
    _validate_tar_member_path(member, dest_path)
    data_filter = getattr(tarfile, "data_filter", None)
    if data_filter is not None:
        return data_filter(member, destination)
    return member


def _extract_archive_safe(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(path=destination, filter=_safe_tar_filter)
    except StateBackupError:
        raise
    except (tarfile.TarError, OSError) as exc:
        raise StateBackupError(
            "STATE_BACKUP_EXTRACT_FAILED",
            f"Failed to extract backup archive safely: {exc}",
            context={"archive": str(archive_path), "destination": str(destination)},
        ) from exc


def _archive_structure(path: Path) -> dict[str, Any]:
    file_count = 0
    total_bytes = 0
    top_level_entries: set[str] = set()
    unsafe_entries: list[str] = []
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            name = member.name
            parts = Path(name).parts
            if not name or name.startswith("/") or ".." in parts:
                unsafe_entries.append(name)
                continue
            if parts:
                top_level_entries.add(parts[0])
            if member.isfile():
                file_count += 1
                total_bytes += int(member.size)
    return {
        "file_count": file_count,
        "total_bytes": total_bytes,
        "top_level_entries": sorted(top_level_entries),
        "unsafe_entries": unsafe_entries,
    }


def _top_level_entries_in_dir(path: Path) -> list[str]:
    if not path.is_dir():
        return []
    return sorted(item.name for item in path.iterdir())


def _yaml_parse_check(root: Path) -> dict[str, Any]:
    yaml_paths = sorted(
        path
        for pattern in ("*.yaml", "*.yml")
        for path in root.rglob(pattern)
        if path.is_file() and not path.is_symlink()
    )
    failures: list[dict[str, str]] = []
    if yaml is None and yaml_paths:
        failures.append({
            "path": "",
            "error": "PyYAML is not available; cannot parse YAML files in backup drill.",
        })
    elif yaml is not None:
        for path in yaml_paths:
            try:
                yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001 - drill should report all parse failures.
                failures.append({
                    "path": path.relative_to(root).as_posix(),
                    "error": str(exc),
                })
    return {
        "name": "yaml_parse",
        "ok": not failures,
        "checked": len(yaml_paths),
        "failures": failures,
    }


def latest_state_backup_manifest(*, backup_root: str | os.PathLike[str] | None = None) -> Path | None:
    listed = list_state_backups(backup_root=backup_root)
    backups = listed.get("backups") or []
    if not backups:
        return None
    manifest = str((backups[0] or {}).get("manifest") or "").strip()
    return Path(manifest) if manifest else None


def _target_or_latest_manifest(
    target: str | os.PathLike[str] | None,
    *,
    backup_root: str | os.PathLike[str] | None = None,
) -> Path:
    if target:
        return _manifest_path_for(target)
    latest = latest_state_backup_manifest(backup_root=backup_root)
    if latest is None:
        raise StateBackupError(
            "STATE_BACKUP_NOT_FOUND",
            "No state backups found.",
            next_actions=["state-backup create --format json"],
        )
    return latest


def _write_drill_evidence(state_root: Path, payload: dict[str, Any]) -> Path:
    evidence_path = _drill_evidence_path(state_root)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    payload["evidence_path"] = str(evidence_path)
    _write_json_0600(evidence_path, payload)
    return evidence_path


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _process_is_zombie(pid: int) -> bool:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    _before, _sep, after = raw.rpartition(")")
    fields = after.split(None, 1)
    return bool(fields) and fields[0] == "Z"


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return not _process_is_zombie(pid)


def _pulse_pid_candidates(state_root: Path, model: dict[str, Any] | None = None) -> list[Path]:
    candidates = [state_root / rel for rel in PULSE_PID_RELS]
    if isinstance(model, dict):
        for log_item in model.get("logs") or []:
            if str(log_item.get("id") or "").strip() == "runtime":
                raw = str(log_item.get("host_path") or "").strip()
                if raw:
                    candidates.append(Path(raw) / "pulse.pid")
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        marker = str(path)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(path)
    return unique


def _running_pulse_pid(state_root: Path, model: dict[str, Any] | None = None) -> dict[str, Any] | None:
    for path in _pulse_pid_candidates(state_root, model):
        pid = _read_pid(path)
        if pid is not None and _process_is_running(pid):
            return {"pid": pid, "pid_file": str(path)}
    return None


def _raise_if_pulse_running(state_root: Path, model: dict[str, Any] | None = None) -> None:
    running = _running_pulse_pid(state_root, model)
    if running is None:
        return
    raise StateBackupError(
        "STATE_BACKUP_PULSE_RUNNING",
        "Refusing state restore while pulse is running.",
        context=running,
        next_actions=["pulse stop", "state-backup restore <manifest> --i-understand-data-loss --format json"],
    )


def _raise_if_verification_failed(verification: dict[str, Any]) -> None:
    if verification.get("ok"):
        return
    checks = verification.get("checks") or []
    sha = next((check for check in checks if check.get("name") == "sha256"), None)
    if sha is not None and not sha.get("ok"):
        raise StateBackupError(
            "STATE_BACKUP_SHA256_MISMATCH",
            "Refusing restore because the backup archive sha256 does not match its manifest.",
            context={"manifest": verification.get("manifest"), "archive": verification.get("archive"), "sha256": sha},
        )
    raise StateBackupError(
        "STATE_BACKUP_VERIFY_FAILED",
        "Refusing restore because backup verification failed.",
        context={"manifest": verification.get("manifest"), "archive": verification.get("archive"), "checks": checks},
    )


def _unique_swap_path(state_root: Path, stamp: str) -> Path:
    for index in range(0, 1000):
        suffix = "" if index == 0 else f"-{index}"
        candidate = state_root.with_name(f".{state_root.name}.pre-restore-{stamp}{suffix}")
        if not candidate.exists():
            return candidate
    raise StateBackupError("STATE_BACKUP_SWAP_NAME_EXHAUSTED", "Could not allocate a restore swap path.")


def verify_state_backup(target: str | os.PathLike[str]) -> dict[str, Any]:
    manifest_path, manifest = _load_manifest(target)
    archive_path = _archive_path_from_manifest(manifest_path, manifest)
    checks: list[dict[str, Any]] = []

    if not archive_path.is_file():
        checks.append({"name": "archive_exists", "ok": False, "message": f"missing archive: {archive_path}"})
        return {
            "ok": False,
            "action": "verify",
            "manifest": str(manifest_path),
            "archive": str(archive_path),
            "checks": checks,
        }

    expected_sha = str(manifest.get("sha256") or "")
    actual_sha = _sha256_file(archive_path)
    checks.append({
        "name": "sha256",
        "ok": bool(expected_sha and actual_sha == expected_sha),
        "expected": expected_sha,
        "actual": actual_sha,
    })

    try:
        structure = _archive_structure(archive_path)
    except (tarfile.TarError, OSError) as exc:
        checks.append({"name": "structure", "ok": False, "message": str(exc)})
        structure = {}
    else:
        checks.append({
            "name": "structure",
            "ok": (
                not structure["unsafe_entries"]
                and int(manifest.get("file_count") or 0) == structure["file_count"]
                and int(manifest.get("total_bytes") or 0) == structure["total_bytes"]
                and list(manifest.get("top_level_entries") or []) == structure["top_level_entries"]
            ),
            "file_count": structure["file_count"],
            "total_bytes": structure["total_bytes"],
            "top_level_entries": structure["top_level_entries"],
            "unsafe_entries": structure["unsafe_entries"],
        })

    ok = all(bool(check.get("ok")) for check in checks)
    return {
        "ok": ok,
        "action": "verify",
        "manifest": str(manifest_path),
        "archive": str(archive_path),
        "created_at": manifest.get("created_at"),
        "source_root": manifest.get("source_root"),
        "archive_bytes": archive_path.stat().st_size if archive_path.is_file() else 0,
        "checks": checks,
    }


def _parse_created_at(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _human_age(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def list_state_backups(
    *,
    backup_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    destination_root = _resolve_backup_root(backup_root)
    destination_root.mkdir(parents=True, exist_ok=True)
    now = _utc_now()
    backups: list[dict[str, Any]] = []

    for manifest_path in sorted(destination_root.glob("*.manifest.json")):
        try:
            _, manifest = _load_manifest(manifest_path)
            archive_path = _archive_path_from_manifest(manifest_path, manifest)
            created_at = _parse_created_at(manifest.get("created_at"))
            verification = verify_state_backup(manifest_path)
            age_seconds = int((now - created_at).total_seconds()) if created_at else None
            backups.append({
                "manifest": str(manifest_path),
                "archive": str(archive_path),
                "archive_name": archive_path.name,
                "created_at": manifest.get("created_at"),
                "age_seconds": max(0, age_seconds) if age_seconds is not None else None,
                "age": _human_age(max(0, age_seconds) if age_seconds is not None else None),
                "archive_bytes": int(manifest.get("archive_bytes") or (archive_path.stat().st_size if archive_path.is_file() else 0)),
                "size": _human_bytes(int(manifest.get("archive_bytes") or (archive_path.stat().st_size if archive_path.is_file() else 0))),
                "file_count": int(manifest.get("file_count") or 0),
                "verified": bool(verification.get("ok")),
                "verify_error": "" if verification.get("ok") else "; ".join(
                    str(check.get("message") or check.get("name") or "")
                    for check in verification.get("checks") or []
                    if not check.get("ok")
                ),
            })
        except Exception as exc:  # noqa: BLE001 - list should report bad rows instead of aborting all backups.
            backups.append({
                "manifest": str(manifest_path),
                "archive": "",
                "archive_name": "",
                "created_at": "",
                "age_seconds": None,
                "age": "unknown",
                "archive_bytes": 0,
                "size": "0 B",
                "file_count": 0,
                "verified": False,
                "verify_error": str(exc),
            })

    backups.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {
        "ok": True,
        "action": "list",
        "backup_root": str(destination_root),
        "count": len(backups),
        "backups": backups,
        "next_actions": ["state-backup create --format json"],
    }


def drill_state_backup(
    target: str | os.PathLike[str] | None = None,
    *,
    state_root: str | os.PathLike[str] | None = None,
    backup_root: str | os.PathLike[str] | None = None,
    model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_root = _resolve_state_root(state_root, model=model)
    manifest_path = _target_or_latest_manifest(target, backup_root=backup_root)
    manifest_path, manifest = _load_manifest(manifest_path)
    archive_path = _archive_path_from_manifest(manifest_path, manifest)
    drilled_at = _isoformat(_utc_now())
    verification = verify_state_backup(manifest_path)
    checks = list(verification.get("checks") or [])
    sha_check = next((check for check in checks if check.get("name") == "sha256"), None)
    sha_ok = bool(sha_check and sha_check.get("ok"))

    if sha_ok:
        with tempfile.TemporaryDirectory(prefix="skillbox-state-drill-") as tmpdir:
            temp_root = Path(tmpdir).resolve()
            try:
                _extract_archive_safe(archive_path, temp_root)
            except StateBackupError as exc:
                checks.append({
                    "name": "path_escape",
                    "ok": False,
                    "message": exc.message,
                    "code": exc.code,
                })
            else:
                checks.append({"name": "path_escape", "ok": True})
                extracted_top = _top_level_entries_in_dir(temp_root)
                expected_top = list(manifest.get("top_level_entries") or [])
                checks.append({
                    "name": "top_level_entries",
                    "ok": extracted_top == expected_top,
                    "expected": expected_top,
                    "actual": extracted_top,
                })
                checks.append(_yaml_parse_check(temp_root))
    else:
        checks.append({
            "name": "extract",
            "ok": False,
            "message": "Skipped extraction because backup sha256 verification failed.",
        })

    ok = all(bool(check.get("ok")) for check in checks)
    payload = {
        "ok": ok,
        "action": "drill",
        "drilled_at": drilled_at,
        "manifest": str(manifest_path),
        "archive": str(archive_path),
        "created_at": manifest.get("created_at"),
        "source_root": manifest.get("source_root"),
        "state_root": str(source_root),
        "checks": checks,
        "next_actions": [
            "state-backup list --format json",
            "stewardship-report <client> --format json",
        ],
    }
    evidence_path = _write_drill_evidence(source_root, payload)
    payload["evidence_path"] = str(evidence_path)
    return payload


def restore_state_backup(
    target: str | os.PathLike[str] | None = None,
    *,
    state_root: str | os.PathLike[str] | None = None,
    backup_root: str | os.PathLike[str] | None = None,
    model: dict[str, Any] | None = None,
    i_understand_data_loss: bool = False,
) -> dict[str, Any]:
    if not i_understand_data_loss:
        raise StateBackupError(
            "STATE_BACKUP_RESTORE_CONFIRMATION_REQUIRED",
            "state-backup restore requires --i-understand-data-loss.",
            next_actions=["state-backup restore <manifest> --i-understand-data-loss --format json"],
        )

    source_root = _resolve_state_root(state_root, model=model)
    destination_root = _resolve_backup_root(backup_root)
    _validate_backup_root(source_root, destination_root)
    manifest_path = _target_or_latest_manifest(target, backup_root=backup_root)
    manifest_path, manifest = _load_manifest(manifest_path)
    archive_path = _archive_path_from_manifest(manifest_path, manifest)

    _raise_if_pulse_running(source_root, model)
    verification = verify_state_backup(manifest_path)
    _raise_if_verification_failed(verification)

    safety = create_state_backup(
        state_root=source_root,
        backup_root=destination_root,
        model=model,
    )

    stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    staging = Path(tempfile.mkdtemp(prefix=f".{source_root.name}.restore-", dir=str(source_root.parent))).resolve()
    previous = _unique_swap_path(source_root, stamp)
    swapped = False
    try:
        _extract_archive_safe(archive_path, staging)
        source_root.rename(previous)
        swapped = True
        staging.rename(source_root)
        shutil.rmtree(previous)
    except Exception:
        if swapped and not source_root.exists() and previous.exists():
            previous.rename(source_root)
        try:
            if staging.exists():
                shutil.rmtree(staging)
        except OSError:
            pass
        raise

    return {
        "ok": True,
        "action": "restore",
        "restored_at": _isoformat(_utc_now()),
        "state_root": str(source_root),
        "manifest": str(manifest_path),
        "archive": str(archive_path),
        "safety_backup": safety.get("backup") or {},
        "checks": verification.get("checks") or [],
        "next_actions": [
            "doctor --format json",
            "state-backup drill --format json",
        ],
    }


def state_backup_text_lines(payload: dict[str, Any]) -> list[str]:
    action = str(payload.get("action") or "")
    if action == "create":
        backup = payload.get("backup") or {}
        return [
            f"created: {backup.get('archive')}",
            f"manifest: {backup.get('manifest')}",
            f"files: {backup.get('file_count', 0)}  bytes: {backup.get('total_bytes', 0)}  archive: {_human_bytes(int(backup.get('archive_bytes') or 0))}",
            f"sha256: {backup.get('sha256')}",
            f"next: {payload.get('next_actions', ['state-backup list --format json'])[0]}",
        ]
    if action == "verify":
        status = "ok" if payload.get("ok") else "failed"
        lines = [
            f"verify: {status}",
            f"archive: {payload.get('archive')}",
            f"manifest: {payload.get('manifest')}",
        ]
        for check in payload.get("checks") or []:
            mark = "ok" if check.get("ok") else "fail"
            detail = check.get("message") or check.get("name") or ""
            lines.append(f"  {mark}: {detail}")
        return lines

    if action == "drill":
        status = "ok" if payload.get("ok") else "failed"
        lines = [
            f"drill: {status}",
            f"archive: {payload.get('archive')}",
            f"evidence: {payload.get('evidence_path')}",
        ]
        for check in payload.get("checks") or []:
            mark = "ok" if check.get("ok") else "fail"
            lines.append(f"  {mark}: {check.get('name')}")
        return lines

    if action == "restore":
        safety = payload.get("safety_backup") or {}
        return [
            "restore: ok",
            f"state_root: {payload.get('state_root')}",
            f"archive: {payload.get('archive')}",
            f"safety_backup: {safety.get('archive')}",
            "next: doctor --format json",
        ]

    backups = payload.get("backups") or []
    lines = [f"backups: {len(backups)}  root: {payload.get('backup_root')}"]
    if not backups:
        lines.append("next: state-backup create --format json")
        return lines
    lines.append(f"{'created_at':<22} {'age':>7} {'size':>10} {'verified':>8} archive")
    for item in backups:
        verified = "yes" if item.get("verified") else "no"
        lines.append(
            f"{str(item.get('created_at') or ''):<22} {str(item.get('age') or ''):>7} "
            f"{str(item.get('size') or ''):>10} {verified:>8} {item.get('archive_name') or item.get('archive')}"
        )
    return lines


__all__ = [
    "BACKUP_ROOT_ENV",
    "DEFAULT_EXCLUDES",
    "DRILL_EVIDENCE_REL",
    "STATE_BACKUP_SCHEMA_VERSION",
    "STATE_ROOT_ENV",
    "StateBackupError",
    "create_state_backup",
    "drill_state_backup",
    "latest_state_backup_manifest",
    "list_state_backups",
    "restore_state_backup",
    "state_backup_text_lines",
    "verify_state_backup",
]
