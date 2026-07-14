"""Capability-negotiated agent attachment adapters for seamless paste."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AdapterDecision:
    schema_version: int
    agent: str
    agent_version: str | None
    state: str
    strategy: str
    visible_attachment: bool
    input_kind: str
    reason: str
    restart_required: bool = False

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


def _version_tuple(raw: str | None) -> tuple[int, ...] | None:
    if not raw:
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    return tuple(map(int, match.groups())) if match else None


def choose_adapter(
    *,
    agent: str,
    agent_version: str | None,
    input_kind: str,
    route_ready: bool,
    native_clipboard: bool = False,
) -> AdapterDecision:
    key = agent.strip().lower()
    if input_kind not in {
        "local_image_path",
        "remote_image_path",
        "inline_image",
        "text",
    }:
        return AdapterDecision(
            SCHEMA_VERSION,
            key,
            agent_version,
            "unsupported",
            "none",
            False,
            input_kind,
            "adapter does not recognize this input kind",
        )
    if input_kind == "text":
        return AdapterDecision(
            SCHEMA_VERSION,
            key,
            agent_version,
            "ready",
            "native_text",
            False,
            input_kind,
            "terminal owns native text paste",
        )
    if key == "codex":
        version = _version_tuple(agent_version)
        if version is None:
            return AdapterDecision(
                SCHEMA_VERSION,
                key,
                agent_version,
                "degraded",
                "text_reference",
                False,
                input_kind,
                "Codex version is unknown; use an explicit path reference",
            )
        if native_clipboard and input_kind == "local_image_path":
            return AdapterDecision(
                SCHEMA_VERSION,
                key,
                agent_version,
                "ready",
                "native_attachment",
                True,
                input_kind,
                "Codex composer can attach the readable local image path",
            )
        if route_ready and input_kind == "remote_image_path":
            return AdapterDecision(
                SCHEMA_VERSION,
                key,
                agent_version,
                "ready",
                "path_paste_attachment",
                True,
                input_kind,
                "Codex TUI recognizes a pasted readable image path as an attachment",
            )
        return AdapterDecision(
            SCHEMA_VERSION,
            key,
            agent_version,
            "degraded",
            "text_reference",
            False,
            input_kind,
            "image path is not readable in the agent filesystem",
        )
    if key == "claude":
        return AdapterDecision(
            SCHEMA_VERSION,
            key,
            agent_version,
            "degraded" if route_ready else "unsupported",
            "path_text_reference" if route_ready else "none",
            False,
            input_kind,
            "Claude terminal attachment behavior is not claimed as native",
        )
    return AdapterDecision(
        SCHEMA_VERSION,
        key or "generic",
        agent_version,
        "degraded" if route_ready else "unsupported",
        "path_text_reference" if route_ready else "none",
        False,
        input_kind,
        "generic terminal agents receive an explicit path reference only",
    )
