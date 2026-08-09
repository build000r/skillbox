"""Last-scan cache for ``sbp git`` (the ``sbp-git/v1`` envelope).

A full estate scan (~100 repos) costs a couple of seconds -- too slow to run
implicitly from ambient surfaces like the bare ``sbp`` home view. Every live
``sbp git`` scan therefore write-throughs its exact ``--json`` envelope here,
and cheap readers (``sbp git --cached``, the home view git line) replay it
without spawning a single git subprocess.

Contract
--------
* Stored shape: ``{"written_at": <ISO-8601 UTC>, "envelope": <sbp-git/v1
  dict>, "previous"?: {"written_at", "envelope"}}`` at
  ``<state_root>/git-scan/last-scan.json``. ONE prior generation is retained
  in the SAME file (``previous``): a single ``os.replace`` keeps the
  current+previous pair torn-proof (two sibling files cannot be replaced
  atomically together), and :func:`load_scan_cache` reads only
  ``written_at``/``envelope``, so every current-generation reader
  (structure_doctor's git_hygiene gate, ``--cached``, the home view) is
  untouched by the extra key. Legacy single-generation files load fine --
  ``previous`` is simply absent.
* :func:`load_previous_scan` returns the retained prior generation with the
  same validation and ``(envelope, age)`` shape as :func:`load_scan_cache`;
  a legacy file, or an invalid ``previous`` subtree, reads as ABSENT.
* State root resolution matches the Makefile (``${SKILLBOX_STATE_ROOT:-
  ./.skillbox-state}`` relative to the repo root): the ``SKILLBOX_STATE_ROOT``
  env var wins (relative values resolve against the cwd, exactly like
  ``cli._skill_default_review_dir``; the sbp wrapper cds to the skillbox root
  before invoking manage.py, so relative == repo-root-relative in practice),
  else ``<runtime_root>/.skillbox-state``.
* Writes are atomic (tmp file in the same directory + ``os.replace``) so a
  concurrent reader never sees a torn file.
* :func:`load_scan_cache` returns ``(envelope, age_seconds)`` or ``None`` --
  it NEVER raises and NEVER partially parses. Missing file, unreadable file,
  invalid JSON, wrong shape, envelope schema != ``sbp-git/v1``, or a
  missing/unparseable ``written_at`` all read as ABSENT. The schema check is
  the pinned compatibility contract: a future ``sbp-git/v2`` writer must not
  be replayed through v1 renderers.
* Freshness: ``age <= CACHE_TTL_SECONDS`` (10 minutes).

This module is stdlib-only on purpose: the home view imports it on every bare
``sbp`` invocation, and it must never drag in the scan engine (or anything
that could spawn git).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "CACHE_SCHEMA",
    "CACHE_TTL_SECONDS",
    "cache_path",
    "home_view_line",
    "load_previous_scan",
    "load_scan_cache",
    "resolve_state_root",
    "write_scan_cache",
]

#: The only envelope schema this cache will replay. Pinned to git_estate.SCHEMA
#: by test (not by import -- see module docstring).
CACHE_SCHEMA = "sbp-git/v1"

#: Maximum age (seconds) at which a cached scan still counts as "recent".
CACHE_TTL_SECONDS = 600

#: Cache file location under the resolved state root.
CACHE_REL_PATH = Path("git-scan") / "last-scan.json"

STATE_ROOT_ENV = "SKILLBOX_STATE_ROOT"

#: Repo root inferred from this file's location (.env-manager/runtime_manager/
#: -> repo root); identical to shared.DEFAULT_ROOT_DIR without importing the
#: (heavy, non-stdlib) shared module.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def resolve_state_root(runtime_root: str | os.PathLike[str] | None = None) -> Path:
    """Skillbox state root: ``$SKILLBOX_STATE_ROOT`` else ``<runtime_root>/.skillbox-state``.

    Mirrors the Makefile default (``./.skillbox-state`` at the repo root) and
    the env-first behavior of the existing state consumers (state_backup.py,
    cli._skill_default_review_dir). A relative env value resolves against the
    cwd, matching those consumers.
    """
    raw = str(os.environ.get(STATE_ROOT_ENV) or "").strip()
    if raw:
        root = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not root.is_absolute():
            root = Path.cwd() / root
        return root
    base = Path(runtime_root) if runtime_root is not None else _REPO_ROOT
    return base / ".skillbox-state"


def cache_path(runtime_root: str | os.PathLike[str] | None = None) -> Path:
    return resolve_state_root(runtime_root) / CACHE_REL_PATH


def write_scan_cache(
    envelope: dict[str, Any],
    runtime_root: str | os.PathLike[str] | None = None,
    *,
    now: datetime | None = None,
) -> Path:
    """Atomically persist ``envelope`` as the last scan. Raises ``OSError`` on
    write failure -- callers degrade (a failed cache write must never fail the
    scan that produced the envelope).

    Rotation: the generation that was current before this write is retained
    once as ``previous`` (its own ``previous``, if any, is dropped -- exactly
    two generations ever exist). An invalid/absent current file rotates to no
    ``previous`` at all.
    """
    target = cache_path(runtime_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    record: dict[str, Any] = {"written_at": stamp, "envelope": envelope}
    displaced = _valid_generation(_read_cache_json(target))
    if displaced is not None:
        record["previous"] = {
            "written_at": displaced[0],
            "envelope": displaced[1],
        }
    payload = json.dumps(record, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".last-scan.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def _parse_written_at(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _read_cache_json(target: Path) -> Any:
    """Best-effort JSON payload of the cache file; ``None`` on ANY problem."""
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _valid_generation(data: Any) -> tuple[str, dict[str, Any]] | None:
    """``(written_at_raw, envelope)`` when ``data`` is one valid generation.

    A generation is ``{"written_at": parseable ISO, "envelope": dict with
    schema == sbp-git/v1}``; anything else (including a v2 envelope) is
    invalid and reads as ABSENT -- the same pinned contract for the current
    and the ``previous`` generation.
    """
    if not isinstance(data, dict):
        return None
    envelope = data.get("envelope")
    if not isinstance(envelope, dict) or envelope.get("schema") != CACHE_SCHEMA:
        return None
    if _parse_written_at(data.get("written_at")) is None:
        return None
    return str(data["written_at"]), envelope


def _generation_with_age(
    data: Any, now: datetime | None
) -> tuple[dict[str, Any], float] | None:
    generation = _valid_generation(data)
    if generation is None:
        return None
    written_at = _parse_written_at(generation[0])
    assert written_at is not None  # _valid_generation guarantees it
    reference = now or datetime.now(timezone.utc)
    age = max(0.0, (reference - written_at).total_seconds())
    return generation[1], age


def load_scan_cache(
    runtime_root: str | os.PathLike[str] | None = None,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], float] | None:
    """``(envelope, age_seconds)`` for the last scan, or ``None`` when absent.

    ABSENT (never raises, never partially parses): missing file, unreadable
    file, invalid JSON, non-dict payload/envelope, envelope schema !=
    ``sbp-git/v1``, missing or unparseable ``written_at``. A clock that ran
    backwards (future ``written_at``) clamps to age 0 rather than going
    negative. Always the CURRENT generation: the additive ``previous`` key is
    invisible here.
    """
    return _generation_with_age(_read_cache_json(cache_path(runtime_root)), now)


def load_previous_scan(
    runtime_root: str | os.PathLike[str] | None = None,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], float] | None:
    """``(envelope, age_seconds)`` for the retained PRIOR generation, or
    ``None`` when absent.

    Same never-raise / never-partial contract as :func:`load_scan_cache`,
    applied to the ``previous`` subtree: a legacy single-generation file, a
    missing file, or an invalid ``previous`` all read as ABSENT.
    """
    data = _read_cache_json(cache_path(runtime_root))
    if not isinstance(data, dict):
        return None
    return _generation_with_age(data.get("previous"), now)


# --------------------------------------------------------------------------- #
# Ambient rendering (home view line, --cached age formatting)
# --------------------------------------------------------------------------- #

#: Stale/absent message, shared by the home view and `sbp git --cached` tty.
NO_RECENT_SCAN_HOME = "git: no recent scan — sbp git"

#: (label, envelope section, key) for the home line counts, in display order.
_HOME_COUNTS = (
    ("dirty", "summary", "dirty"),
    ("ahead", "summary", "ahead-clean"),
    ("mid-op", "summary", "mid-op"),
    ("diverged", "summary", "diverged-clean"),
    ("unregistered", "registration_summary", "unregistered"),
)


def format_age(age_seconds: float) -> str:
    """``34s`` under a minute, ``4m`` after (TTL keeps this under 10m)."""
    seconds = max(0, int(age_seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m"


def home_view_line(
    runtime_root: str | os.PathLike[str] | None = None,
    *,
    now: datetime | None = None,
) -> str:
    """The ONE ambient git line for the bare ``sbp`` home view.

    Sourced from the cache only -- this function must never scan (stdlib-only
    module; no git, no subprocess). Fresh cache -> nonzero
    dirty/ahead/mid-op/diverged/unregistered counts with the age always
    appended (``git: 7 dirty, 3 ahead, 1 mid-op (4m ago)``); an all-clear
    fresh cache -> ``git: clean (Nm ago)``; stale or absent ->
    ``git: no recent scan — sbp git``.
    """
    loaded = load_scan_cache(runtime_root, now=now)
    if loaded is None:
        return NO_RECENT_SCAN_HOME
    envelope, age = loaded
    if age > CACHE_TTL_SECONDS:
        return NO_RECENT_SCAN_HOME
    parts: list[str] = []
    for label, section, key in _HOME_COUNTS:
        bucket = envelope.get(section)
        count = bucket.get(key, 0) if isinstance(bucket, dict) else 0
        if isinstance(count, int) and count > 0:
            parts.append(f"{count} {label}")
    body = ", ".join(parts) if parts else "clean"
    return f"git: {body} ({format_age(age)} ago)"
