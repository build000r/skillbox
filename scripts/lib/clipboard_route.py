"""Canonical profile, alias, and clipboard-capability routing for Skillbox."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


CAPABILITY_KEYS = (
    "osc52_copy",
    "smart_path_paste",
    "native_image_paste",
)
BROKER_STRATEGIES = {"local", "negotiated", "none"}
DISPLAY_STRATEGIES = {"native", "xvfb", "none"}
TRUST_LEVELS = {"local", "allowlisted", "explicit", "unsupported"}
SAFE_SSH_TARGET = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+$")


class HostConfigError(ValueError):
    """Raised when tracked host routing is ambiguous or contradictory."""


def validate_ssh_target(target: str) -> str:
    if target.startswith("-") or SAFE_SSH_TARGET.fullmatch(target) is None:
        raise HostConfigError("unsafe ssh_target")
    return target


def load_host_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_host_config(data)
    return data


def validate_host_config(data: dict[str, Any]) -> None:
    profiles = data.get("profiles")
    aliases = data.get("aliases")
    if not isinstance(profiles, dict) or not profiles:
        raise HostConfigError("profiles must be a non-empty object")
    if not isinstance(aliases, dict):
        raise HostConfigError("aliases must be an object")

    ssh_targets: dict[str, str] = {}
    for name, raw in profiles.items():
        if not isinstance(raw, dict):
            raise HostConfigError(f"profile {name!r} must be an object")
        capabilities = raw.get("capabilities")
        if not isinstance(capabilities, dict):
            raise HostConfigError(f"profile {name!r} is missing capabilities")
        for direction in ("outbound", "inbound"):
            if not isinstance(capabilities.get(direction), dict):
                raise HostConfigError(
                    f"profile {name!r} capabilities.{direction} must be an object"
                )
        flattened = {**capabilities["outbound"], **capabilities["inbound"]}
        missing = [key for key in CAPABILITY_KEYS if key not in flattened]
        if missing:
            raise HostConfigError(
                f"profile {name!r} missing capabilities: {', '.join(missing)}"
            )
        if raw.get("broker_strategy") not in BROKER_STRATEGIES:
            raise HostConfigError(f"profile {name!r} has invalid broker_strategy")
        if raw.get("display_strategy") not in DISPLAY_STRATEGIES:
            raise HostConfigError(f"profile {name!r} has invalid display_strategy")
        if raw.get("trust") not in TRUST_LEVELS:
            raise HostConfigError(f"profile {name!r} has invalid trust")
        supported = any(bool(value) for value in flattened.values())
        if not supported and not raw.get("unsupported_reason"):
            raise HostConfigError(
                f"unsupported profile {name!r} requires unsupported_reason"
            )
        target = raw.get("ssh_target")
        if target:
            if not isinstance(target, str):
                raise HostConfigError(f"profile {name!r} has unsafe ssh_target")
            try:
                validate_ssh_target(target)
            except HostConfigError as exc:
                raise HostConfigError(
                    f"profile {name!r} has unsafe ssh_target"
                ) from exc
            previous = ssh_targets.get(target)
            if previous and not raw.get("shares_target_with") == previous:
                raise HostConfigError(
                    f"profiles {previous!r} and {name!r} share ssh_target {target!r} without declaration"
                )
            ssh_targets[target] = name

    for alias, profile in aliases.items():
        if not alias or alias != alias.strip().lower():
            raise HostConfigError(f"alias {alias!r} must be normalized lowercase")
        if profile not in profiles:
            raise HostConfigError(
                f"alias {alias!r} references unknown profile {profile!r}"
            )
        if alias in profiles and alias != profile:
            raise HostConfigError(
                f"alias {alias!r} contradicts canonical profile {alias!r}"
            )


def resolve_alias(alias: str, data: dict[str, Any]) -> str:
    key = alias.strip().lower()
    if key in data["profiles"]:
        return key
    return data["aliases"].get(key, key)


def resolve_profile(
    profile_or_alias: str,
    *,
    data: dict[str, Any],
    target: str | None = None,
) -> dict[str, Any]:
    key = resolve_alias(profile_or_alias, data)
    profiles = data["profiles"]
    if key not in profiles:
        if target or "@" in profile_or_alias or "." in profile_or_alias:
            target = target or profile_or_alias
            key = "generic"
        else:
            raise HostConfigError(
                f"unknown profile or alias {profile_or_alias!r}; supported: {', '.join(sorted(profiles))}"
            )
    entry = dict(profiles[key])
    entry["capabilities"] = {
        direction: dict(values) for direction, values in entry["capabilities"].items()
    }
    entry["profile"] = key
    if target:
        entry["ssh_target"] = validate_ssh_target(target)
    if entry.get("dynamic_target") and not entry.get("ssh_target"):
        raise HostConfigError(f"profile {key!r} requires an explicit target")
    if key == "generic" and not entry.get("ssh_target"):
        raise HostConfigError("generic profile requires an explicit target")
    return entry


def capability(entry: dict[str, Any], name: str) -> bool:
    caps = entry["capabilities"]
    return bool(
        caps.get("inbound", {}).get(name, caps.get("outbound", {}).get(name, False))
    )
