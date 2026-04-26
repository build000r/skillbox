"""Doctor checks for distributor-backed skill sync state."""
from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from ..shared import CheckResult, file_sha256, load_skill_repos_config
from .http_security import HttpsOnlyError, require_https, secure_opener
from ..shared_distribution import (
    ConfigError,
    DistributorConfig,
    parse_distribution_config,
)
from .lockfile import LockfileSchemaError, parse_lockfile
from .manifest import ManifestSchemaError, parse_manifest, verify_manifest
from .signing import KeyFormatError, SignatureVerificationError, load_public_key


@dataclass(frozen=True)
class _DistributionDoctorContext:
    root_dir: Path
    distributors: dict[str, DistributorConfig]
    lock_paths: list[Path]
    parse_issues: list[str]


def _state_root(root_dir: Path) -> Path:
    return root_dir / ".skillbox-state"


def _distribution_context(model: dict[str, Any]) -> _DistributionDoctorContext:
    raw_root = str(model.get("root_dir") or "").strip()
    root_dir = Path(raw_root).resolve() if raw_root else Path.cwd()
    distributors: dict[str, DistributorConfig] = {}
    lock_paths: set[Path] = set()
    parse_issues: list[str] = []

    for skillset in model.get("skills") or []:
        if skillset.get("kind") != "skill-repo-set":
            continue

        skillset_id = str(skillset.get("id") or "(missing id)")
        config_path = Path(str(skillset.get("skill_repos_config_host_path") or ""))
        if not config_path.is_file():
            continue

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                config = load_skill_repos_config(config_path)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                parsed_distributors, sources = parse_distribution_config(config, config_path)
        except (RuntimeError, ConfigError) as exc:
            parse_issues.append(f"{skillset_id}: {exc}")
            continue

        if not parsed_distributors and not sources:
            continue

        raw_lock_path = str(skillset.get("lock_path_host_path") or "").strip()
        if raw_lock_path:
            lock_paths.add(Path(raw_lock_path))

        for distributor_id, parsed in parsed_distributors.items():
            existing = distributors.get(distributor_id)
            if existing is None:
                distributors[distributor_id] = parsed
                continue
            if existing != parsed:
                parse_issues.append(
                    f"{skillset_id}: conflicting definitions for distributor '{distributor_id}'"
                )

    return _DistributionDoctorContext(
        root_dir=root_dir,
        distributors=distributors,
        lock_paths=sorted(lock_paths),
        parse_issues=parse_issues,
    )


def _no_distributor_result(code: str) -> CheckResult:
    return CheckResult(
        status="pass",
        code=code,
        message="no distributor configured",
    )


def _result_for_parse_issues(code: str, message: str, parse_issues: list[str]) -> CheckResult:
    return CheckResult(
        status="warn",
        code=code,
        message=message,
        details={"issues": parse_issues},
    )


def _check_distributor_config_valid(context: _DistributionDoctorContext) -> CheckResult:
    if context.parse_issues:
        return CheckResult(
            status="fail",
            code="distributor_config_valid",
            message="distributor config is invalid",
            details={"issues": context.parse_issues},
        )

    if not context.distributors:
        return _no_distributor_result("distributor_config_valid")

    missing_env: list[str] = []
    for distributor_id, distributor in sorted(context.distributors.items()):
        key_env = str(distributor.auth.key_env or "").strip()
        if not key_env or not str(os.environ.get(key_env) or "").strip():
            missing_env.append(f"{distributor_id}: missing env {key_env!r}")

    details = {"distributors": sorted(context.distributors)}
    if missing_env:
        details["issues"] = missing_env
        return CheckResult(
            status="warn",
            code="distributor_config_valid",
            message="distributor auth env vars are missing",
            details=details,
        )

    return CheckResult(
        status="pass",
        code="distributor_config_valid",
        message="distributor config parsed successfully",
        details=details,
    )


def _probe_manifest_head(distributor: DistributorConfig, api_key: str) -> tuple[bool, str]:
    url = f"{distributor.url.rstrip('/')}/manifest"
    try:
        require_https(url)
    except HttpsOnlyError as exc:
        return False, f"{distributor.id}: {exc}"
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Client-ID": distributor.client_id,
            "Accept": "application/json",
        },
        method="HEAD",
    )
    try:
        with secure_opener().open(request, timeout=5.0) as response:
            status = int(getattr(response, "status", response.getcode()))
    except HTTPError as exc:
        status = int(exc.code)
        if status in {200, 304}:
            return True, f"{distributor.id}: HEAD {status} {url}"
        return False, f"{distributor.id}: HEAD {status} {url}"
    except (URLError, OSError) as exc:
        return False, f"{distributor.id}: network error probing {url}: {exc}"
    if status in {200, 304}:
        return True, f"{distributor.id}: HEAD {status} {url}"
    return False, f"{distributor.id}: HEAD {status} {url}"


def _check_distributor_auth_probe(context: _DistributionDoctorContext) -> CheckResult:
    if context.parse_issues:
        return _result_for_parse_issues(
            "distributor_auth_probe",
            "distributor auth probe skipped because config is invalid",
            context.parse_issues,
        )

    if not context.distributors:
        return _no_distributor_result("distributor_auth_probe")

    issues: list[str] = []
    for distributor_id, distributor in sorted(context.distributors.items()):
        key_env = str(distributor.auth.key_env or "").strip()
        api_key = str(os.environ.get(key_env) or "").strip()
        if not api_key:
            issues.append(f"{distributor_id}: missing env {key_env!r}; probe skipped")
            continue
        ok, detail = _probe_manifest_head(distributor, api_key)
        if not ok:
            issues.append(detail)

    if issues:
        return CheckResult(
            status="warn",
            code="distributor_auth_probe",
            message="distributor auth probe could not verify one or more endpoints",
            details={"issues": issues},
        )

    return CheckResult(
        status="pass",
        code="distributor_auth_probe",
        message="distributor auth probe succeeded",
        details={"distributors": sorted(context.distributors)},
    )


def _check_distributor_manifest_signature(context: _DistributionDoctorContext) -> CheckResult:
    if context.parse_issues:
        return _result_for_parse_issues(
            "distributor_manifest_signature",
            "manifest signature check skipped because config is invalid",
            context.parse_issues,
        )

    if not context.distributors:
        return _no_distributor_result("distributor_manifest_signature")

    manifest_dir = _state_root(context.root_dir) / "manifests"
    failures: list[str] = []
    warnings_: list[str] = []

    for distributor_id, distributor in sorted(context.distributors.items()):
        manifest_path = manifest_dir / f"{distributor_id}.json"
        if not manifest_path.is_file():
            warnings_.append(f"{distributor_id}: cached manifest missing at {manifest_path}")
            continue

        try:
            manifest = parse_manifest(manifest_path.read_bytes())
            public_key = load_public_key(distributor.verification.public_key)
            verify_manifest(manifest, public_key)
            if manifest.distributor_id != distributor_id:
                failures.append(
                    f"{distributor_id}: manifest distributor_id {manifest.distributor_id!r} does not match config id"
                )
        except (
            OSError,
            ValueError,
            KeyFormatError,
            ManifestSchemaError,
            SignatureVerificationError,
        ) as exc:
            failures.append(f"{distributor_id}: signature verification failed: {exc}")

    if failures:
        return CheckResult(
            status="fail",
            code="distributor_manifest_signature",
            message="cached distributor manifest signature verification failed",
            details={"issues": failures},
        )
    if warnings_:
        return CheckResult(
            status="warn",
            code="distributor_manifest_signature",
            message="cached distributor manifests are missing",
            details={"issues": warnings_},
        )
    return CheckResult(
        status="pass",
        code="distributor_manifest_signature",
        message="cached distributor manifest signatures are valid",
    )


def _load_lockfile_paths(context: _DistributionDoctorContext) -> list[Path]:
    return context.lock_paths


def _check_distributor_bundle_cache_integrity(context: _DistributionDoctorContext) -> CheckResult:
    if context.parse_issues:
        return _result_for_parse_issues(
            "distributor_bundle_cache_integrity",
            "bundle cache integrity check skipped because config is invalid",
            context.parse_issues,
        )

    if not context.distributors:
        return _no_distributor_result("distributor_bundle_cache_integrity")

    bundle_cache_root = _state_root(context.root_dir) / "bundle-cache"
    issues: list[str] = []
    checked = 0

    for lock_path in _load_lockfile_paths(context):
        if not lock_path.is_file():
            issues.append(f"lockfile missing at {lock_path}")
            continue
        try:
            raw = json.loads(lock_path.read_text(encoding="utf-8"))
            lockfile = parse_lockfile(raw)
        except (OSError, json.JSONDecodeError, LockfileSchemaError) as exc:
            issues.append(f"failed to parse lockfile {lock_path}: {exc}")
            continue

        for entry in lockfile.skills:
            if entry.source != "distributor":
                continue
            if entry.distributor_id and entry.distributor_id not in context.distributors:
                continue

            checked += 1
            if entry.version is None:
                issues.append(f"{entry.name}: distributor lock entry missing version")
                continue
            if not str(entry.bundle_sha256 or "").strip():
                issues.append(f"{entry.name}: distributor lock entry missing bundle_sha256")
                continue

            bundle_path = (
                bundle_cache_root
                / entry.name
                / f"{entry.name}-v{entry.version}.skillbundle.tar.gz"
            )
            if not bundle_path.is_file():
                issues.append(f"{entry.name}: cached bundle missing at {bundle_path}")
                continue

            actual_sha = file_sha256(bundle_path)
            if actual_sha != entry.bundle_sha256:
                issues.append(
                    f"{entry.name}: cached bundle hash mismatch "
                    f"(expected {entry.bundle_sha256}, got {actual_sha})"
                )

    if issues:
        return CheckResult(
            status="warn",
            code="distributor_bundle_cache_integrity",
            message="distributor bundle cache integrity issues detected",
            details={"issues": issues},
        )

    if checked == 0:
        return CheckResult(
            status="pass",
            code="distributor_bundle_cache_integrity",
            message="no distributor bundle cache entries to validate",
        )

    return CheckResult(
        status="pass",
        code="distributor_bundle_cache_integrity",
        message="distributor bundle cache hashes match lockfile records",
        details={"checked_entries": checked},
    )


def _check_distributor_lockfile_consistency(context: _DistributionDoctorContext) -> CheckResult:
    if context.parse_issues:
        return _result_for_parse_issues(
            "distributor_lockfile_consistency",
            "distributor lockfile consistency check skipped because config is invalid",
            context.parse_issues,
        )

    if not context.distributors:
        return _no_distributor_result("distributor_lockfile_consistency")

    manifest_dir = _state_root(context.root_dir) / "manifests"
    issues: list[str] = []
    references_checked = 0

    for lock_path in _load_lockfile_paths(context):
        if not lock_path.is_file():
            issues.append(f"lockfile missing at {lock_path}")
            continue
        try:
            raw = json.loads(lock_path.read_text(encoding="utf-8"))
            lockfile = parse_lockfile(raw)
        except (OSError, json.JSONDecodeError, LockfileSchemaError) as exc:
            issues.append(f"failed to parse lockfile {lock_path}: {exc}")
            continue

        for distributor_id in sorted(lockfile.distributor_manifests):
            references_checked += 1
            if distributor_id not in context.distributors:
                issues.append(
                    f"lockfile {lock_path} references unknown distributor {distributor_id!r}"
                )
            manifest_path = manifest_dir / f"{distributor_id}.json"
            if not manifest_path.is_file():
                issues.append(
                    f"lockfile {lock_path} references {distributor_id!r} "
                    f"but cached manifest is missing at {manifest_path}"
                )

    if issues:
        return CheckResult(
            status="warn",
            code="distributor_lockfile_consistency",
            message="distributor lockfile references are inconsistent with cached manifests",
            details={"issues": issues},
        )

    if references_checked == 0:
        return CheckResult(
            status="pass",
            code="distributor_lockfile_consistency",
            message="no distributor lockfile manifest references to validate",
        )

    return CheckResult(
        status="pass",
        code="distributor_lockfile_consistency",
        message="distributor lockfile references are consistent with cached manifests",
        details={"checked_references": references_checked},
    )


def validate_distribution_doctor_checks(model: dict[str, Any]) -> list[CheckResult]:
    context = _distribution_context(model)
    return [
        _check_distributor_config_valid(context),
        _check_distributor_auth_probe(context),
        _check_distributor_manifest_signature(context),
        _check_distributor_bundle_cache_integrity(context),
        _check_distributor_lockfile_consistency(context),
    ]
