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

from .manifest import (
    ClientManifestArtifact,
    ClientManifestSkill,
    artifact_for_version,
)

DISTRIBUTION_ARTIFACT_NOT_AVAILABLE = "DISTRIBUTION_ARTIFACT_NOT_AVAILABLE"


class PinResolutionError(Exception):
    pass


@dataclass(frozen=True)
class PinResolution:
    version: int
    pinned_by: str
    reason: str | None = None
    artifact: ClientManifestArtifact | None = None


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

    artifact = _selected_artifact(skill, version)

    if version > effective:
        return PinResolution(
            version=version,
            pinned_by="manifest_floor",
            reason=skill.min_version_reason,
            artifact=artifact,
        )

    if client_pin is not None and client_pin < recommended:
        return PinResolution(
            version=version,
            pinned_by="client",
            artifact=artifact,
        )

    return PinResolution(
        version=version,
        pinned_by="manifest_recommendation",
        artifact=artifact,
    )


def _selected_artifact(
    skill: ClientManifestSkill,
    version: int,
) -> ClientManifestArtifact | None:
    if not skill.artifacts:
        return None
    artifact = artifact_for_version(skill, version)
    if artifact is None:
        raise PinResolutionError(
            f"{DISTRIBUTION_ARTIFACT_NOT_AVAILABLE}: "
            f"{skill.name} version {version} has no artifact"
        )
    return artifact
