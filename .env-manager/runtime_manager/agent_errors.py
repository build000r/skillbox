"""Shared error envelope helpers for agent-ops brain payloads."""
from __future__ import annotations

from typing import Any, Mapping

from .errors import SkillboxError


def brain_error_payload(
    schema_version: str,
    code: str,
    message: str,
    *,
    context: Mapping[str, Any] | None = None,
    next_actions: list[str] | None = None,
    suggestions: list[dict[str, Any]] | None = None,
    recoverable: bool = True,
) -> dict[str, Any]:
    """Render a typed envelope while preserving brain legacy affordances."""
    error_context: dict[str, Any] = dict(context or {})
    if suggestions:
        error_context["suggestions"] = suggestions

    payload = SkillboxError(
        code,
        message,
        context=error_context,
        next_actions=next_actions or (),
        recoverable=recoverable,
    ).to_payload()
    payload["schema_version"] = schema_version

    # Legacy brain consumers currently read error.details and/or top-level
    # suggestions. Keep both during the same deprecation window as error.type
    # and top-level error_code.
    if context:
        payload["error"]["details"] = dict(context)
    if suggestions:
        payload["suggestions"] = list(suggestions)
    return payload


__all__ = ["brain_error_payload"]
