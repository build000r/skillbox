"""Typed error hierarchy + one JSON envelope for the runtime manager.

This module is the single CARRIER for runtime errors. It does NOT renumber or
rename any existing error code: the string ``code`` carried by a typed raise is
exactly the legacy code (``LOCAL_RUNTIME_*``, ``PERSISTENCE_*``, or the
``error.type`` value ``classify_error`` already produces). The type tree is
DELIBERATELY shallow — new distinctions go in ``code``, not new subclasses.

``SkillboxError.to_payload()`` renders ONE envelope that is, for one release,
both the new shape AND the legacy shape so downstream tooling can migrate:

    {
        "ok": false,
        "error": {
            "code": "<stable code>",          # new canonical field
            "message": "...",
            "context": {...},                  # structured, optional
            "next_actions": [...],
            "type": "<stable code>",           # legacy mirror of code
            "recoverable": true                # legacy field
        },
        "error_code": "<stable code>",         # legacy top-level mirror
        "next_actions": [...],                 # legacy top-level mirror
        "deprecation": {                       # migration marker
            "legacy_keys": ["error.type", "error.recoverable", "error_code"],
            "use_instead": ["error.code", "error.context", "error.next_actions"],
            "note": "..."
        }
    }

The ``error.code`` / ``error.type`` / top-level ``error_code`` always agree, so
a snapshot test enumerating known codes pins them together.
"""

from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping


# Migration note surfaced verbatim in every typed envelope. Kept as a module
# constant so the deprecation marker is a single source of truth (and a stable
# golden) rather than re-typed at each render site.
DEPRECATION_NOTE = (
    "Top-level error/error_code and error.type/error.recoverable are legacy "
    "mirrors retained for one release. Read error.code, error.context, and "
    "error.next_actions instead."
)

DEPRECATION_MARKER: dict[str, Any] = {
    "legacy_keys": ["error.type", "error.recoverable", "error_code"],
    "use_instead": ["error.code", "error.context", "error.next_actions"],
    "note": DEPRECATION_NOTE,
}


class SkillboxError(RuntimeError):
    """Base for every typed runtime error.

    Inherits from ``RuntimeError`` so that, for one release, every existing
    ``except RuntimeError`` site (and ``classify_error``) keeps catching these
    typed raises unchanged — this is a back-compat CARRIER change, not a
    catch-site migration. The top-level handler checks ``isinstance(exc,
    SkillboxError)`` FIRST and renders ``to_payload()`` directly; only legacy
    bare ``RuntimeError`` raisers still route through ``classify_error``.

    Carries a stable string ``code`` (unchanged from the legacy codes), a human
    ``message``, optional structured ``context``, ``next_actions`` hints, and a
    ``recoverable`` flag mirrored into the legacy envelope.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        context: Mapping[str, Any] | None = None,
        next_actions: Iterable[str] = (),
        recoverable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.context: dict[str, Any] = dict(context) if context else {}
        self.next_actions: list[str] = [str(action) for action in next_actions]
        self.recoverable = bool(recoverable)

    def to_payload(self) -> dict[str, Any]:
        """Render the back-compat envelope (new shape + legacy mirrors)."""
        error_obj: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            # Legacy mirrors so existing consumers that key off error.type /
            # error.recoverable keep working during the deprecation window.
            "type": self.code,
            "recoverable": self.recoverable,
        }
        if self.context:
            error_obj["context"] = dict(self.context)
        if self.next_actions:
            error_obj["next_actions"] = list(self.next_actions)
        payload: dict[str, Any] = {
            "ok": False,
            "error": error_obj,
            # Legacy top-level mirrors.
            "error_code": self.code,
            # Deep-copy so a caller mutating the rendered payload never corrupts
            # the shared module-level marker.
            "deprecation": copy.deepcopy(DEPRECATION_MARKER),
        }
        if self.next_actions:
            payload["next_actions"] = list(self.next_actions)
        return payload

    def __str__(self) -> str:
        return self.message


class ValidationError(SkillboxError):
    """Input/config/contract violation (bad arg, malformed manifest/lockfile)."""


class RuntimeLifecycleError(SkillboxError):
    """Failure starting/stopping/bootstrapping runtime services or tasks."""


class StateConflictError(SkillboxError):
    """Conflicting/blocked runtime state (cycles, blocked deps, already-exists)."""


class AdapterError(SkillboxError):
    """A downstream tool/adapter (git, archive extract, subprocess) failed."""


class NetworkError(SkillboxError):
    """A network operation failed (download digest mismatch, fetch failure)."""


# Generic fallback code for unknown/unclassified exceptions surfaced by the
# top-level handler. Distinct from the per-domain codes so an INTERNAL envelope
# is never mistaken for a known failure.
INTERNAL_ERROR_CODE = "INTERNAL"


def internal_error_payload(
    message: str,
    *,
    context: Mapping[str, Any] | None = None,
    next_actions: Iterable[str] = (),
) -> dict[str, Any]:
    """Envelope for an unexpected, unclassified exception (generic INTERNAL)."""
    return SkillboxError(
        INTERNAL_ERROR_CODE,
        message,
        context=context,
        next_actions=next_actions,
        recoverable=False,
    ).to_payload()


__all__ = [
    "DEPRECATION_MARKER",
    "DEPRECATION_NOTE",
    "INTERNAL_ERROR_CODE",
    "SkillboxError",
    "ValidationError",
    "RuntimeLifecycleError",
    "StateConflictError",
    "AdapterError",
    "NetworkError",
    "internal_error_payload",
]
