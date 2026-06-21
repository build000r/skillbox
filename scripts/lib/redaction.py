"""Single source of truth for secret redaction across skillbox surfaces.

This module is a *leaf* — it imports only the standard library so it can be
imported the same way as :mod:`lib.runtime_model` from both ``scripts/*`` and
the ``runtime_manager`` package (which re-exports it via
``runtime_manager/shared.py``). Before this module existed, four surfaces
(``agent_adapters``, ``agent_snapshots``, ``mcp_server``, and
``operator_mcp_server``) carried their own copies of the
``TOKEN|SECRET|PASSWORD|...`` pattern table, which drifted; meanwhile
``scripts/box.py`` redacted nothing, so a Tailscale authkey or DigitalOcean
token in remote ``doctl``/``ssh`` stderr landed verbatim in operator JSON and
transcripts.

Public API
----------
``redact_text(s) -> str``
    Redact secret-looking substrings in a single string. Never raises;
    coerces non-string input to ``str`` first.
``redact_value(obj) -> obj``
    Recurse over dict/list/tuple, redacting string values (and whole values
    whose *key* matches the sensitive-name set), leaving keys and structure
    intact. Never raises.

Design properties (exercised by ``tests/test_redaction.py``)
------------------------------------------------------------
* **Idempotent** — ``redact_text(redact_text(x)) == redact_text(x)``. The
  marker ``[REDACTED]`` contains no characters that the value-position
  patterns match, so a second pass is a no-op.
* **Never raises** on weird input (``None``, ``bytes``, ints, deeply nested
  structures, cyclic-ish reuse) — every branch coerces or short-circuits.
* **Value-targeted, not word-targeted** — patterns redact the *value* in a
  ``KEY=value`` / ``Bearer value`` / ``scheme://user:pass@host`` shape or a
  provider-prefixed credential token. A file literally named ``token.txt`` or
  the bare word ``secret`` / ``password`` in prose is NOT mangled.
"""
from __future__ import annotations

import re
from typing import Any

__all__ = [
    "REDACTION_MARKER",
    "SECRET_KEY_PATTERN",
    "is_secret_key",
    "redact_text",
    "redact_value",
]

REDACTION_MARKER = "[REDACTED]"

# Sensitive KEY names, used both for KEY=VALUE assignments and for whole-value
# redaction by mapping key. UNION of the four prior copies' key sets
# (TOKEN/SECRET/PASSWORD/PASSWD/API_KEY/AUTH_KEY/PRIVATE_KEY/ACCESS_KEY) plus
# the bead's explicit AUTHKEY (already covered by AUTH[_-]?KEY, kept explicit
# for clarity) and CREDENTIAL/PASSPHRASE which appear in secret-path heuristics
# elsewhere in the tree.
SECRET_KEY_PATTERN = (
    r"TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|"
    r"API[_-]?KEY|AUTH[_-]?KEY|AUTHKEY|"
    r"PRIVATE[_-]?KEY|ACCESS[_-]?KEY|CREDENTIAL"
)

# A mapping key whose NAME signals a secret -> redact the whole value.
# (Matches the operator/snapshot recursion semantics: redact by key, not just
# by the string's own shape.)
_SECRET_KEY_RE = re.compile(rf"(?:{SECRET_KEY_PATTERN})", re.IGNORECASE)

# KEY=VALUE / KEY: VALUE where KEY contains a sensitive name. Value position is
# group(2); we keep the leading "KEY=" / opening quote (group 1) and any
# trailing quote (group 3) so the surrounding shape stays readable.
# (UNION: identical across all four prior copies.)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"("
    r"(?:\b|[\"'])"
    rf"[A-Z0-9_.-]*(?:{SECRET_KEY_PATTERN})[A-Z0-9_.-]*"
    r"(?:\b|[\"'])"
    r"\s*[:=]\s*"
    r"[\"']?"
    r")"
    r"([^\"'\s,;]+)"
    r"([\"']?)",
    re.IGNORECASE,
)

# Authorization: Bearer <token> (UNION: identical across all four prior copies).
_BEARER_TOKEN_RE = re.compile(
    r"(\b(?:authorization|proxy-authorization)\s*:\s*bearer\s+)([^\s,;]+)",
    re.IGNORECASE,
)

# URL userinfo: scheme://user:pass@host -> scheme://[REDACTED]@host.
# Only fires when a password (":pass") is present, so a bare "user@host"
# (e.g. an ssh target or an email-looking token) is left alone. (NEW.)
_URL_USERINFO_RE = re.compile(
    r"([a-z][a-z0-9+.-]*://)"          # scheme://
    r"([^/@\s:]+:[^/@\s]+)"            # user:pass (password required)
    r"(@)",                            # @host
    re.IGNORECASE,
)

# Provider-prefixed credential tokens. These are unambiguous secret shapes that
# can appear in remote output WITHOUT a KEY= around them (e.g. a tailscale
# enrollment line or a doctl error echoing a token). (NEW.)
#   - Tailscale auth keys:        tskey-...
#   - DigitalOcean PATs/tokens:   dop_v1_..., doo_v1_..., dor_v1_...
_PROVIDER_TOKEN_RE = re.compile(
    r"\b("
    r"tskey-[A-Za-z0-9._-]+"
    r"|do[opr]_v1_[A-Za-z0-9]+"
    r")",
)


def is_secret_key(key: Any) -> bool:
    """Return True when *key*'s NAME signals a secret value.

    Used by :func:`redact_value` to redact whole values by mapping key, and
    reusable by callers that key-redact structured payloads. Never raises.
    """
    try:
        return bool(_SECRET_KEY_RE.search(str(key)))
    except Exception:
        return False


def redact_text(s: Any) -> str:
    """Redact secret-looking substrings in *s*, returning a string.

    Order matters only for readability, not correctness: each pass targets a
    disjoint value shape and the marker is inert to every pattern, so the
    function is idempotent. Non-string input is coerced via ``str`` so the
    function never raises on ``None``/``bytes``/ints/etc.
    """
    if not isinstance(s, str):
        if s is None:
            return ""
        try:
            s = s.decode("utf-8", "replace") if isinstance(s, (bytes, bytearray)) else str(s)
        except Exception:
            return REDACTION_MARKER
    text = _BEARER_TOKEN_RE.sub(lambda m: f"{m.group(1)}{REDACTION_MARKER}", s)
    text = _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}{REDACTION_MARKER}{m.group(3)}", text)
    text = _PROVIDER_TOKEN_RE.sub(REDACTION_MARKER, text)
    text = _SECRET_ASSIGNMENT_RE.sub(
        lambda m: f"{m.group(1)}{REDACTION_MARKER}{m.group(3)}",
        text,
    )
    return text


def redact_value(obj: Any, *, _key: Any = "", _depth: int = 0) -> Any:
    """Recursively redact secret-looking string values within *obj*.

    * Mapping values whose KEY name signals a secret are replaced wholesale
      with the marker (matching the prior snapshot/operator recursion).
    * Plain string values are passed through :func:`redact_text`.
    * dict/list/tuple are recursed; keys and structure are preserved (tuples
      become lists, as in the prior snapshot recursion, to stay JSON-safe).
    * Everything else (ints, floats, bools, None, unknown objects) is returned
      unchanged.

    Never raises. A defensive depth guard stops pathologically deep or
    self-referential structures from exhausting the stack.
    """
    if _depth > 200:
        return REDACTION_MARKER if is_secret_key(_key) else obj
    if is_secret_key(_key):
        return REDACTION_MARKER
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            out[k] = redact_value(v, _key=k, _depth=_depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_value(item, _key=_key, _depth=_depth + 1) for item in obj]
    return obj
