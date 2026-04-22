"""Pure-function pin resolution for skillbox distribution manifests.

Pin resolution rule::

    installed = max(
        manifest_skill.min_version or 0,
        min(
            manifest_skill.version,
            client_pin or manifest_skill.version
        )
    )

The ``pinned_by`` tag records which constraint determined the final version.
"""
from __future__ import annotations

from dataclasses import dataclass

from .manifest import ClientManifestSkill


@dataclass(frozen=True)
class PinResolution:
    version: int
    pinned_by: str
    reason: str | None = None


def resolve_pin(
    skill: ClientManifestSkill,
    client_pin: int | None = None,
) -> PinResolution:
    recommended = skill.version
    floor = skill.min_version or 0

    if client_pin is not None:
        effective = min(recommended, client_pin)
    else:
        effective = recommended

    version = max(floor, effective)

    if version > effective:
        return PinResolution(
            version=version,
            pinned_by="manifest_floor",
            reason=skill.min_version_reason,
        )

    if client_pin is not None and client_pin < recommended:
        return PinResolution(
            version=version,
            pinned_by="client",
        )

    return PinResolution(
        version=version,
        pinned_by="manifest_recommendation",
    )
