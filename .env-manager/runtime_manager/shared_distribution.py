"""Distribution-related config schema parsing helpers for skill-repos.yaml."""
from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    """Raised when distribution-specific config schema validation fails."""


@dataclass(frozen=True)
class DistributorAuth:
    method: str
    key_env: str


@dataclass(frozen=True)
class DistributorVerification:
    public_key: str


@dataclass(frozen=True)
class DistributorConfig:
    id: str
    url: str
    client_id: str
    auth: DistributorAuth
    verification: DistributorVerification


@dataclass(frozen=True)
class DistributorSetSource:
    distributor: str
    pick: list[str]
    pin: dict[str, int]


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping")
    return value


def _require_non_empty_str(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ConfigError(f"{label} must be a non-empty string")
    return text


def _parse_pick(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{label}.pick must be a list")
    result: list[str] = []
    for idx, item in enumerate(value):
        result.append(_require_non_empty_str(item, f"{label}.pick[{idx}]"))
    return result


def _parse_pin(value: Any, label: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{label}.pin must be a mapping of skill name -> integer version")
    result: dict[str, int] = {}
    for skill_name, raw_version in value.items():
        normalized_skill = _require_non_empty_str(skill_name, f"{label}.pin key")
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise ConfigError(f"{label}.pin.{normalized_skill} must be an integer")
        if raw_version < 0:
            raise ConfigError(f"{label}.pin.{normalized_skill} must be >= 0")
        result[normalized_skill] = raw_version
    return result


def parse_distributors(raw_config: dict[str, Any], config_path: Path) -> dict[str, DistributorConfig]:
    raw_distributors = raw_config.get("distributors")
    if raw_distributors is None:
        return {}
    if not isinstance(raw_distributors, list):
        raise ConfigError(f"distributors must be a list in {config_path}")

    parsed: dict[str, DistributorConfig] = {}
    for idx, raw_entry in enumerate(raw_distributors):
        label = f"distributors[{idx}]"
        entry = _require_mapping(raw_entry, label)

        distributor_id = _require_non_empty_str(entry.get("id"), f"{label}.id")
        if distributor_id in parsed:
            raise ConfigError(f"{label}.id duplicates distributor '{distributor_id}'")
        url = _require_non_empty_str(entry.get("url"), f"{label}.url")
        client_id = _require_non_empty_str(entry.get("client_id"), f"{label}.client_id")

        raw_auth = _require_mapping(entry.get("auth"), f"{label}.auth")
        method = _require_non_empty_str(raw_auth.get("method"), f"{label}.auth.method")
        if method != "api-key":
            raise ConfigError(
                f"{label}.auth.method unsupported value {method!r}; expected 'api-key'"
            )
        key_env = _require_non_empty_str(raw_auth.get("key_env"), f"{label}.auth.key_env")

        raw_verification = _require_mapping(entry.get("verification"), f"{label}.verification")
        public_key = _require_non_empty_str(
            raw_verification.get("public_key"),
            f"{label}.verification.public_key",
        )

        distributor = DistributorConfig(
            id=distributor_id,
            url=url,
            client_id=client_id,
            auth=DistributorAuth(method=method, key_env=key_env),
            verification=DistributorVerification(public_key=public_key),
        )
        parsed[distributor_id] = distributor

        # Key lookup remains sync-time; this is a best-effort warning only.
        if os.environ.get(key_env) is None:
            warnings.warn(
                (
                    f"distributor '{distributor_id}' auth env var '{key_env}' is not set; "
                    "sync may fail until it is exported"
                ),
                RuntimeWarning,
                stacklevel=2,
            )

    return parsed


def parse_distributor_sources(raw_config: dict[str, Any], config_path: Path) -> list[DistributorSetSource]:
    raw_entries = raw_config.get("skill_repos")
    if raw_entries is None:
        return []
    if not isinstance(raw_entries, list):
        raise ConfigError(f"skill_repos must be a list in {config_path}")

    sources: list[DistributorSetSource] = []
    for idx, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            raise ConfigError(f"skill_repos[{idx}] must be a mapping")
        if "distributor" not in raw_entry:
            continue

        has_repo = bool(raw_entry.get("repo"))
        has_path = bool(raw_entry.get("path"))
        has_distributor = bool(raw_entry.get("distributor"))
        if sum(int(flag) for flag in (has_repo, has_path, has_distributor)) != 1:
            raise ConfigError(
                f"skill_repos[{idx}] with distributor source must declare only 'distributor'"
            )

        distributor_id = _require_non_empty_str(raw_entry.get("distributor"), f"skill_repos[{idx}].distributor")
        sources.append(
            DistributorSetSource(
                distributor=distributor_id,
                pick=_parse_pick(raw_entry.get("pick"), f"skill_repos[{idx}]"),
                pin=_parse_pin(raw_entry.get("pin"), f"skill_repos[{idx}]"),
            )
        )

    return sources


def validate_distributor_refs(
    sources: list[DistributorSetSource],
    distributors: dict[str, DistributorConfig],
) -> None:
    for idx, source in enumerate(sources):
        if source.distributor not in distributors:
            raise ConfigError(
                f"skill_repos[{idx}] references unknown distributor '{source.distributor}'"
            )


def parse_distribution_config(
    raw_config: dict[str, Any],
    config_path: Path,
) -> tuple[dict[str, DistributorConfig], list[DistributorSetSource]]:
    distributors = parse_distributors(raw_config, config_path)
    sources = parse_distributor_sources(raw_config, config_path)
    validate_distributor_refs(sources, distributors)
    return distributors, sources


def validate_distribution_config(raw_config: dict[str, Any], config_path: Path) -> None:
    parse_distribution_config(raw_config, config_path)
