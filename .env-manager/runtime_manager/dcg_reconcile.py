"""Atomic, merge-safe DCG hook convergence for Claude, Codex, and Grok homes.

This module is the ONE supported way Skillbox turns a managed home (a host
account or a persistent in-box ``/home/sandbox``) into a home where every agent
runs commands through the pinned destructive-command guard. It converges four
artifacts and nothing else:

===================  ===========================================================
Artifact             Path (relative to ``--home``)
===================  ===========================================================
Claude Code hook     ``.claude/settings.json``  ``hooks.PreToolUse[]``
Codex CLI hook       ``.codex/hooks.json``      ``hooks.PreToolUse[]``
Grok native hook     ``.grok/hooks/dcg.json``   (whole file, upstream shape)
User DCG policy      ``.config/dcg/config.toml`` (rendered by ``dcg_policy``)
===================  ===========================================================

Non-goals (owned by other modules): downloading or verifying the binary
(:mod:`runtime_manager.dcg_distribution` owns the pin), authoring the policy
(:mod:`runtime_manager.dcg_policy` owns the text), and lifecycle shell wiring.

The five rules this module exists to enforce
--------------------------------------------
1. **Merge-safe.** These are *someone else's* config files. Anything this module
   did not put there survives: unrelated top-level keys, unrelated hook events,
   and unrelated ``PreToolUse`` entries (Claude's ``rch`` compilation hook is the
   canonical example). Existing formatting is preserved too — the writer reuses
   the file's own indentation and trailing-newline convention, and a file that
   needs no semantic change is not rewritten at all.
2. **Atomic.** Convergence is planned across all agents while doing zero writes,
   then committed as one batch of ``os.replace`` calls with a full pre-image
   backup. A failure mid-batch restores every file already replaced. There is no
   state in which one agent is converged and another is half-written.
3. **Refuse, never half-write.** A config that does not parse (JSON or TOML) is
   never repaired, reformatted, or overwritten: planning raises, the run exits
   nonzero, and *every* file — including the ones this module could have written
   — keeps its original bytes.
4. **Never bypass Codex trust.** Codex will not run a hook whose hash it has not
   persisted (``hooks.state."<id>".trusted_hash`` in ``.codex/config.toml``).
   This module PREPARES ``.codex/hooks.json`` and DETECTS that persisted trust;
   it never writes a trust hash, never edits ``.codex/config.toml`` at all, and
   :data:`BYPASS_FLAG` (``--dangerously-bypass-hook-trust``) is rejected as an
   argument and asserted absent from everything this module generates. Absent or
   stale trust is reported as ``needs-operator-action``, never ``healthy``.
5. **Reversible.** ``apply`` records a backup set; ``rollback`` restores the home
   to its pre-apply bytes and removes the backup set it consumed. ``relinquish``
   removes only DCG-owned entries, links, and marker-stamped policy files.

Outcome vocabulary
------------------
Per-artifact ``state`` is one of ``changed``, ``healthy``, ``needs-operator-action``,
``unsupported``, ``failed``. The top-level ``result`` describes the *mutation*
(``changed`` / ``unchanged`` / ``removed`` / ``rolled-back`` / ``failed``) and the
top-level ``status`` describes *health* (``healthy`` / ``needs-operator-action`` /
``unsupported`` / ``failed``). Exit codes: 0 healthy, 1 failed, 2
needs-operator-action, 3 unsupported.

CLI::

    python3 -m runtime_manager.dcg_reconcile apply      --home H --binary B --format json
    python3 -m runtime_manager.dcg_reconcile verify     --home H --format json
    python3 -m runtime_manager.dcg_reconcile relinquish --home H --format json
    python3 -m runtime_manager.dcg_reconcile rollback   --home H --format json

``--home`` is mandatory on every action. This module can rewrite live agent
configuration, so it never guesses a home from the environment: a caller that
means ``$HOME`` has to say so.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import dcg_distribution as _dist
from . import dcg_policy as _policy
from .errors import SkillboxError, ValidationError

__all__ = [
    "BYPASS_FLAG",
    "CLAUDE_SETTINGS_RELPATH",
    "CODEX_CONFIG_RELPATH",
    "CODEX_HOOKS_RELPATH",
    "DEFAULT_BINARY_RELPATH",
    "GROK_DESCRIPTION",
    "GROK_HOOK_RELPATH",
    "HOOK_EVENT",
    "HOOK_MATCHER",
    "LEDGER_RELPATH",
    "Layout",
    "POLICY_RELPATH",
    "RECONCILE_SCHEMA_VERSION",
    "RESULT_CHANGED",
    "RESULT_FAILED",
    "RESULT_REMOVED",
    "RESULT_ROLLED_BACK",
    "RESULT_UNCHANGED",
    "STATE_CHANGED",
    "STATE_FAILED",
    "STATE_HEALTHY",
    "STATE_NEEDS_OPERATOR",
    "STATE_UNSUPPORTED",
    "apply",
    "claude_hook_entry",
    "claude_matcher_group",
    "grok_hook_document",
    "is_dcg_command",
    "layout",
    "main",
    "relinquish",
    "rollback",
    "verify",
]


RECONCILE_SCHEMA_VERSION = 1

# The Codex flag that makes hooks run without persisted trust. It is never
# accepted as an argument, never emitted, and never written into a config.
BYPASS_FLAG = "--dangerously-bypass-hook-trust"

CLAUDE_SETTINGS_RELPATH = ".claude/settings.json"
CODEX_HOOKS_RELPATH = ".codex/hooks.json"
CODEX_CONFIG_RELPATH = ".codex/config.toml"
GROK_HOOK_RELPATH = ".grok/hooks/dcg.json"
POLICY_RELPATH = ".config/dcg/config.toml"
LEDGER_RELPATH = ".config/dcg/skillbox-reconcile.json"
BACKUPS_RELPATH = ".config/dcg/backups"
DEFAULT_BINARY_RELPATH = ".local/bin/dcg"

HOOK_EVENT = "PreToolUse"
HOOK_MATCHER = "Bash"

# Verbatim from `dcg install --grok` (v0.6.7) so a Skillbox-converged Grok home
# is byte-comparable with an upstream-installed one.
GROK_DESCRIPTION = (
    "dcg (Destructive Command Guard) — blocks rm -rf, git reset --hard, force "
    "pushes, DROP DATABASE, kubectl delete, and similar destructive commands "
    "before Grok's run_terminal_cmd tool can execute them."
)
GROK_HOOK_TIMEOUT = 5

STATE_CHANGED = "changed"
STATE_HEALTHY = "healthy"
STATE_NEEDS_OPERATOR = "needs-operator-action"
STATE_UNSUPPORTED = "unsupported"
STATE_FAILED = "failed"

RESULT_CHANGED = "changed"
RESULT_UNCHANGED = "unchanged"
RESULT_REMOVED = "removed"
RESULT_ROLLED_BACK = "rolled-back"
RESULT_FAILED = "failed"

# Family exit-code ladder. Source of truth:
# ``runtime_manager._shared.errors`` (EXIT_OK/EXIT_ERROR/EXIT_USAGE/
# EXIT_NEEDS_INPUT/EXIT_DRIFT). ``main()`` builds an ``argparse`` parser, so 2
# is RESERVED for argparse usage errors on every path we do not route through
# our own error handling — "needs operator action" therefore CANNOT live on 2
# (a caller gating on ``$? -eq 2`` could not tell "your invocation was wrong"
# from "I need you to do something"). It moves to 3 = EXIT_NEEDS_INPUT, whose
# published meaning is exactly "operator input required".
#
# ``unsupported`` has no family slot (it is neither an error, nor drift, nor a
# request for input: this host simply cannot run the reconcile), so it moves
# ABOVE the reserved family range rather than squatting on 3. Codes >= 5 are
# tool-local by construction.
# tests/test_dcg_reconcile.py pins these against the real family constants.
EXIT_OK = 0
EXIT_FAILED = 1
# 2 is reserved family-wide for argparse usage errors — deliberately unused here.
EXIT_NEEDS_OPERATOR = 3
EXIT_UNSUPPORTED = 5

DCG_RECONCILE_MALFORMED_CONFIG = "DCG_RECONCILE_MALFORMED_CONFIG"
DCG_RECONCILE_WRITE_FAILED = "DCG_RECONCILE_WRITE_FAILED"
DCG_RECONCILE_NO_BACKUP = "DCG_RECONCILE_NO_BACKUP"
DCG_RECONCILE_BYPASS_FORBIDDEN = "DCG_RECONCILE_BYPASS_FORBIDDEN"
DCG_RECONCILE_BAD_ARGUMENT = "DCG_RECONCILE_BAD_ARGUMENT"
DCG_RECONCILE_SITE_POLICY_REQUIRED = "DCG_RECONCILE_SITE_POLICY_REQUIRED"
DCG_RECONCILE_POLICY_ADOPTION_MISMATCH = "DCG_RECONCILE_POLICY_ADOPTION_MISMATCH"

CODEX_TRUST_ABSENT = "absent"
CODEX_TRUST_STALE = "stale"
CODEX_TRUST_TRUSTED = "trusted"

CODEX_TRUST_ACTION = (
    "Start Codex in this home and trust the dcg hook from its hook review modal "
    f"(Codex persists the hash itself). Never pass {BYPASS_FLAG}."
)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """Every path this module is allowed to touch, derived from one home."""

    home: Path
    binary: Path
    default_binary: Path
    claude_settings: Path
    codex_hooks: Path
    codex_config: Path
    grok_hook: Path
    policy_config: Path
    ledger: Path
    backups_root: Path

    def relative(self, path: Path) -> str:
        try:
            return str(Path(path).relative_to(self.home))
        except ValueError:
            return str(path)


def layout(home: Path | str, binary: Path | str | None = None) -> Layout:
    """Resolve the managed-home layout. Never reads the environment."""
    home_path = Path(home).expanduser()
    default_binary = home_path / DEFAULT_BINARY_RELPATH
    binary_path = Path(binary).expanduser() if binary else default_binary
    return Layout(
        home=home_path,
        binary=binary_path,
        default_binary=default_binary,
        claude_settings=home_path / CLAUDE_SETTINGS_RELPATH,
        codex_hooks=home_path / CODEX_HOOKS_RELPATH,
        codex_config=home_path / CODEX_CONFIG_RELPATH,
        grok_hook=home_path / GROK_HOOK_RELPATH,
        policy_config=home_path / POLICY_RELPATH,
        ledger=home_path / LEDGER_RELPATH,
        backups_root=home_path / BACKUPS_RELPATH,
    )


# ---------------------------------------------------------------------------
# Hook shapes and ownership
# ---------------------------------------------------------------------------

# A hook entry is DCG-owned when its command invokes a program whose basename is
# exactly `dcg`. This deliberately adopts hooks installed by upstream
# `dcg install` (bare `dcg`), by an older Skillbox render (the
# `command -v dcg ... && dcg || true` wrapper), and by an absolute path — which
# is why convergence collapses them into ONE entry instead of adding a duplicate.
# It never matches `rch`, `/opt/dcg-tools/run.sh`, or `dcgfoo`.
_DCG_COMMAND_RE = re.compile(r"(?:^|[\s;&|()])(?:[^\s;&|()]*/)?dcg(?=$|[\s;&|()])")


def is_dcg_command(command: Any) -> bool:
    """True when ``command`` invokes the DCG binary in any supported spelling."""
    if not isinstance(command, str) or not command.strip():
        return False
    return bool(_DCG_COMMAND_RE.search(command))


def claude_hook_entry(binary: Path | str) -> dict[str, Any]:
    """The canonical command entry Claude/Codex run before a Bash tool call."""
    return {"type": "command", "command": str(binary)}


def claude_matcher_group(binary: Path | str) -> dict[str, Any]:
    """The canonical ``PreToolUse`` matcher group for Claude and Codex."""
    return {"matcher": HOOK_MATCHER, "hooks": [claude_hook_entry(binary)]}


def _grok_hook_entry(binary: Path | str) -> dict[str, Any]:
    return {"type": "command", "command": str(binary), "timeout": GROK_HOOK_TIMEOUT}


def grok_hook_document(binary: Path | str) -> dict[str, Any]:
    """The whole ``~/.grok/hooks/dcg.json`` document, upstream shape."""
    return {
        "description": GROK_DESCRIPTION,
        "hooks": {HOOK_EVENT: [{"matcher": HOOK_MATCHER, "hooks": [_grok_hook_entry(binary)]}]},
    }


# ---------------------------------------------------------------------------
# Formatting-preserving JSON IO
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _JsonFile:
    """A parsed JSON config plus the formatting needed to write it back."""

    path: Path
    raw: str | None
    document: Any
    indent: int
    trailing_newline: bool

    @property
    def exists(self) -> bool:
        return self.raw is not None


_INDENT_RE = re.compile(r"^( +)\S", re.MULTILINE)


def _detect_indent(text: str) -> int:
    match = _INDENT_RE.search(text)
    return len(match.group(1)) if match else 2


def _malformed(path: Path, detail: str) -> ValidationError:
    return ValidationError(
        DCG_RECONCILE_MALFORMED_CONFIG,
        f"{path} is malformed and was left untouched: {detail}",
        context={"path": str(path), "detail": detail},
        next_actions=[
            f"Inspect {path} by hand; DCG convergence refuses to rewrite a config it cannot parse.",
        ],
        recoverable=True,
    )


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except IsADirectoryError as exc:
        raise _malformed(path, "expected a file, found a directory") from exc
    except UnicodeDecodeError as exc:
        raise _malformed(path, f"not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise _malformed(path, f"unreadable: {exc}") from exc


def _load_json_file(path: Path) -> _JsonFile:
    """Parse a JSON config. A parse failure REFUSES the whole run."""
    raw = _read_text(path)
    if raw is None:
        return _JsonFile(path=path, raw=None, document=None, indent=2, trailing_newline=True)
    if not raw.strip():
        raise _malformed(path, "file is empty")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _malformed(path, f"not valid JSON: {exc}") from exc
    return _JsonFile(
        path=path,
        raw=raw,
        document=document,
        indent=_detect_indent(raw),
        trailing_newline=raw.endswith("\n"),
    )


def _dump_json(document: Any, *, indent: int, trailing_newline: bool) -> bytes:
    text = json.dumps(document, indent=indent, ensure_ascii=False)
    if trailing_newline:
        text += "\n"
    return text.encode("utf-8")


def _load_toml_file(path: Path) -> tuple[str | None, Mapping[str, Any]]:
    raw = _read_text(path)
    if raw is None:
        return None, {}
    try:
        return raw, tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise _malformed(path, f"not valid TOML: {exc}") from exc


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Document convergence (Claude / Codex / Grok all share the hook document shape)
# ---------------------------------------------------------------------------


class _Unsupported(Exception):
    """The file parses but is not the documented hook shape: never mutate it."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass
class _MergeReport:
    document: Any
    changed: bool
    adopted: int = 0
    duplicates_removed: int = 0
    created_hooks: bool = False
    created_event: bool = False


def _hook_entries(group: Any) -> list[Any]:
    entries = group.get("hooks")
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise _Unsupported("a PreToolUse matcher group has a non-list 'hooks' value")
    return entries


def _is_owned_entry(entry: Any, owned_commands: frozenset[str] = frozenset()) -> bool:
    """Is this hook entry ours?

    Two ways to qualify, and both are needed. The command SHAPE catches hooks
    installed by upstream ``dcg install`` or an older Skillbox render, which is
    what prevents duplicates. The exact command STRING catches a pinned binary
    that is not literally named ``dcg`` (``dcg-0.6.7``, a versioned store path);
    without it a rename would make convergence append a fresh copy on every run.
    """
    if not isinstance(entry, Mapping) or entry.get("type") != "command":
        return False
    command = entry.get("command")
    if is_dcg_command(command):
        return True
    return isinstance(command, str) and command.strip() in owned_commands


def _event_list(document: Any, *, create: bool) -> tuple[list[Any], bool, bool]:
    """Return ``(PreToolUse list, created_hooks, created_event)``."""
    if not isinstance(document, dict):
        raise _Unsupported("top-level value is not a JSON object")
    hooks = document.get("hooks")
    created_hooks = False
    if hooks is None:
        if not create:
            return [], False, False
        hooks = {}
        document["hooks"] = hooks
        created_hooks = True
    if not isinstance(hooks, dict):
        raise _Unsupported("'hooks' is not an object")
    events = hooks.get(HOOK_EVENT)
    created_event = False
    if events is None:
        if not create:
            return [], created_hooks, False
        events = []
        hooks[HOOK_EVENT] = events
        created_event = True
    if not isinstance(events, list):
        raise _Unsupported(f"'hooks.{HOOK_EVENT}' is not an array")
    for group in events:
        if not isinstance(group, dict):
            raise _Unsupported(f"'hooks.{HOOK_EVENT}' contains a non-object entry")
        _hook_entries(group)
    return events, created_hooks, created_event


def _converge_document(
    document: Any,
    *,
    binary: Path | str,
    grok: bool = False,
    owned_commands: frozenset[str] = frozenset(),
) -> _MergeReport:
    """Merge exactly one canonical DCG hook into ``document``.

    Existing DCG entries are ADOPTED (updated in place, unrelated keys kept) and
    de-duplicated. Everything else in the document is untouched.
    """
    document = json.loads(json.dumps(document)) if document is not None else {}
    events, created_hooks, created_event = _event_list(document, create=True)
    want_entry = _grok_hook_entry(binary) if grok else claude_hook_entry(binary)
    owned_commands = owned_commands | {str(binary)}

    changed = created_hooks or created_event
    adopted = 0
    duplicates = 0
    first_owner: tuple[dict[str, Any], dict[str, Any]] | None = None

    for group in list(events):
        if group.get("matcher") != HOOK_MATCHER:
            continue
        entries = _hook_entries(group)
        group_removed = 0
        for entry in list(entries):
            if not _is_owned_entry(entry, owned_commands):
                continue
            if first_owner is None:
                first_owner = (group, entry)
                adopted += 1
                continue
            entries.remove(entry)
            group_removed += 1
            duplicates += 1
            changed = True
        # Only a group THIS pass emptied is dropped; a group that was already
        # empty, or that carries keys we do not own, is left exactly as found.
        if group_removed and not entries and set(group) <= {"matcher", "hooks"}:
            events.remove(group)

    if first_owner is None:
        group = {"matcher": HOOK_MATCHER, "hooks": [dict(want_entry)]}
        events.append(group)
        changed = True
    else:
        _group, entry = first_owner
        for key, value in want_entry.items():
            if entry.get(key) != value:
                entry[key] = value
                changed = True

    if grok and document.get("description") != GROK_DESCRIPTION:
        # Rebuild with `description` first so a fresh file matches upstream order.
        rest = {key: value for key, value in document.items() if key != "description"}
        document = {"description": GROK_DESCRIPTION, **rest}
        changed = True

    return _MergeReport(
        document=document,
        changed=changed,
        adopted=adopted,
        duplicates_removed=duplicates,
        created_hooks=created_hooks,
        created_event=created_event,
    )


def _strip_owned(
    document: Any,
    *,
    created_hooks: bool,
    created_event: bool,
    owned_commands: frozenset[str] = frozenset(),
) -> tuple[Any, bool, int]:
    """Remove every DCG-owned entry. Returns ``(document, changed, removed)``."""
    document = json.loads(json.dumps(document)) if document is not None else {}
    events, _created_hooks, _created_event = _event_list(document, create=False)
    removed = 0
    changed = False
    for group in list(events):
        if group.get("matcher") != HOOK_MATCHER:
            continue
        entries = _hook_entries(group)
        group_removed = 0
        for entry in list(entries):
            if _is_owned_entry(entry, owned_commands):
                entries.remove(entry)
                group_removed += 1
                removed += 1
                changed = True
        if group_removed and not entries and set(group) <= {"matcher", "hooks"}:
            events.remove(group)
            changed = True

    hooks = document.get("hooks") if isinstance(document, dict) else None
    if isinstance(hooks, dict):
        if created_event and hooks.get(HOOK_EVENT) == []:
            hooks.pop(HOOK_EVENT)
            changed = True
        if created_hooks and hooks == {}:
            document.pop("hooks")
            changed = True
    return document, changed, removed


def _unrelated_view(
    document: Any,
    *,
    grok: bool = False,
    owned_commands: frozenset[str] = frozenset(),
) -> Any:
    """The document with every DCG-owned entry and empty container pruned.

    Two documents with the same view differ ONLY in DCG-owned content, which is
    exactly what ``unrelated_preserved`` asserts. For the Grok hook file the
    upstream ``description`` is DCG-owned too, so it is pruned there and only
    there.
    """
    if document is None:
        return None
    try:
        stripped, _changed, _removed = _strip_owned(
            document,
            created_hooks=True,
            created_event=True,
            owned_commands=owned_commands,
        )
    except _Unsupported:
        return document
    if not isinstance(stripped, dict):
        return stripped
    hooks = stripped.get("hooks")
    if isinstance(hooks, dict):
        if hooks.get(HOOK_EVENT) == []:
            hooks.pop(HOOK_EVENT)
        if hooks == {}:
            stripped.pop("hooks")
    if grok:
        stripped.pop("description", None)
    return stripped or None


# ---------------------------------------------------------------------------
# Ledger (this module's own bookkeeping — never an agent's file)
# ---------------------------------------------------------------------------


def _empty_ledger() -> dict[str, Any]:
    return {
        "schema_version": RECONCILE_SCHEMA_VERSION,
        "agents": {},
        "policy": {},
        "binary_link": {},
        "codex_trust": {},
        "backups": {"next_id": 1, "last": ""},
    }


def _load_ledger(path: Path) -> dict[str, Any]:
    raw = _read_text(path)
    if raw is None:
        return _empty_ledger()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _malformed(path, f"reconcile ledger is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise _malformed(path, "reconcile ledger is not a JSON object")
    merged = _empty_ledger()
    merged.update(document)
    return merged


def _ledger_bytes(document: Mapping[str, Any]) -> bytes:
    # Sorted keys, no timestamps, no hostnames: a converged home re-serializes
    # byte-identically on every subsequent run.
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Write batch: plan everything, then commit atomically
# ---------------------------------------------------------------------------


@dataclass
class _Write:
    path: Path
    data: bytes | None  # None => delete
    prior: bytes | None
    existed: bool

    @property
    def target(self) -> Path:
        # Follow symlinks so a symlinked config farm keeps its links: replace the
        # link TARGET, never the link itself.
        path = self.path
        if path.is_symlink():
            resolved = path.resolve()
            return resolved
        return path

    @property
    def is_noop(self) -> bool:
        if self.data is None:
            return not self.existed
        return self.existed and self.prior == self.data


def _file_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise _malformed(path, f"unreadable: {exc}") from exc


def _plan_write(path: Path, data: bytes | None) -> _Write:
    prior = _file_bytes(path)
    return _Write(path=path, data=data, prior=prior, existed=prior is not None)


def _write_backup(layout_: Layout, writes: Sequence[_Write], backup_id: str) -> dict[str, Any]:
    backup_dir = layout_.backups_root / backup_id
    blobs = backup_dir / "files"
    blobs.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for index, write in enumerate(writes):
        entry: dict[str, Any] = {
            "path": layout_.relative(write.path),
            "existed": write.existed,
        }
        if write.existed and write.prior is not None:
            blob = blobs / f"{index:03d}.blob"
            blob.write_bytes(write.prior)
            entry["blob"] = blob.name
            entry["sha256"] = _sha256_bytes(write.prior)
        entries.append(entry)
    manifest = {"id": backup_id, "schema_version": RECONCILE_SCHEMA_VERSION, "files": entries}
    (backup_dir / "manifest.json").write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    return manifest


def _commit(writes: Sequence[_Write]) -> list[Path]:
    """Stage every write, then replace them all. Restore everything on failure."""
    staged: list[tuple[_Write, Path | None]] = []
    for write in writes:
        if write.data is None:
            staged.append((write, None))
            continue
        target = write.target
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{target.name}.dcg-reconcile.tmp"
        with open(tmp, "wb") as handle:
            handle.write(write.data)
            handle.flush()
            os.fsync(handle.fileno())
        staged.append((write, tmp))

    done: list[_Write] = []
    try:
        for write, tmp in staged:
            if tmp is None:
                target = write.target
                if target.exists() or target.is_symlink():
                    target.unlink()
            else:
                os.replace(tmp, write.target)
            done.append(write)
    except OSError as exc:
        for write in done:
            target = write.target
            if write.existed and write.prior is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(write.prior)
            elif target.exists():
                target.unlink()
        for _write, tmp in staged:
            if tmp is not None and tmp.exists():
                tmp.unlink()
        raise SkillboxError(
            DCG_RECONCILE_WRITE_FAILED,
            f"DCG convergence failed mid-batch and was rolled back: {exc}",
            context={"error": str(exc)},
            recoverable=True,
        ) from exc
    return [write.path for write in writes]


# ---------------------------------------------------------------------------
# Binary + policy + Codex trust
# ---------------------------------------------------------------------------


def _binary_report(layout_: Layout, pin: Any) -> dict[str, Any]:
    path = layout_.binary
    report: dict[str, Any] = {
        "path": str(path),
        "expected_version": _dist.DCG_VERSION,
        "asset": pin.asset,
        "sha256": pin.sha256,
        "minisign_key_id": pin.minisign_key_id,
        "installed_version": "",
        "state": STATE_NEEDS_OPERATOR,
        "detail": "",
    }
    if not path.is_file():
        report["detail"] = "dcg binary is not installed"
        report["operator_action"] = (
            "Install the pinned binary with the distribution contract "
            "(runtime_manager.dcg_distribution.install_verified_binary); this module never downloads."
        )
        return report
    try:
        installed = _dist.installed_version(path)
    except _dist.InstalledVersionError as exc:
        report["detail"] = exc.message
        report["operator_action"] = "Reinstall the pinned binary via the distribution contract."
        return report
    report["installed_version"] = installed
    if installed != _dist.DCG_VERSION:
        report["detail"] = f"installed {installed}, pinned {_dist.DCG_VERSION}"
        report["operator_action"] = "Replace the binary via the distribution contract."
        return report
    report["state"] = STATE_HEALTHY
    report["detail"] = f"pinned {_dist.DCG_VERSION} verified on disk"
    return report


def _codex_trusted_hash(layout_: Layout, hooks_document: Any) -> str:
    """Read Codex's PERSISTED hook trust. Read-only; never writes config.toml.

    Codex records trust as ``hooks.state."<id>".trusted_hash`` in
    ``~/.codex/config.toml`` (and, for some builds, as a ``trusted_hash`` key on
    the hook group inside ``hooks.json``). The id scheme is Codex's business, so
    both sources are collapsed into one sorted fingerprint: it is empty when no
    trust is persisted and it CHANGES whenever Codex re-trusts.
    """
    hashes: list[str] = []
    _raw, document = _load_toml_file(layout_.codex_config)
    state = ((document.get("hooks") or {}) if isinstance(document, Mapping) else {}).get("state")
    if isinstance(state, Mapping):
        for key in sorted(state):
            entry = state[key]
            if not isinstance(entry, Mapping):
                continue
            if entry.get("enabled") is False:
                continue
            value = entry.get("trusted_hash")
            if isinstance(value, str) and value.strip():
                hashes.append(f"{key}={value.strip()}")
    if isinstance(hooks_document, Mapping):
        events = ((hooks_document.get("hooks") or {})).get(HOOK_EVENT)
        if isinstance(events, list):
            for group in events:
                if not isinstance(group, Mapping):
                    continue
                value = group.get("trusted_hash")
                if isinstance(value, str) and value.strip():
                    hashes.append(f"hooks.json={value.strip()}")
    return ",".join(sorted(set(hashes)))


def _codex_trust_state(
    *,
    ledger_trust: Mapping[str, Any],
    observed: str,
    digest: str,
    mutated: bool,
) -> tuple[str, dict[str, Any]]:
    """Decide trust state and the ledger record to persist.

    * we just rewrote ``hooks.json`` -> any pre-existing trust is INVALIDATED
    * no persisted hash at all                    -> ``absent``
    * the persisted hash is the one we invalidated -> ``stale``
    * otherwise                                    -> ``trusted``
    """
    if mutated:
        record = {
            "hooks_sha256": digest,
            "trusted_hash": "",
            "invalidated_hash": observed,
        }
        return (CODEX_TRUST_ABSENT if not observed else CODEX_TRUST_STALE), record
    if not observed:
        return CODEX_TRUST_ABSENT, {"hooks_sha256": digest, "trusted_hash": "", "invalidated_hash": ""}
    invalidated = str(ledger_trust.get("invalidated_hash") or "")
    if invalidated and invalidated == observed and str(ledger_trust.get("hooks_sha256") or "") == digest:
        return CODEX_TRUST_STALE, dict(ledger_trust)
    return CODEX_TRUST_TRUSTED, {
        "hooks_sha256": digest,
        "trusted_hash": observed,
        "invalidated_hash": "",
    }


def _policy_text(policy: _policy.DcgPolicy) -> str:
    return _policy.render_policy(policy)


def _site_policy_receipt(sites: Sequence[_policy.DcgSitePolicy]) -> dict[str, Any]:
    """Metadata safe for ledgers and command output; never rule bodies or paths."""
    return {
        "required": bool(sites),
        "sites": [site.to_receipt() for site in sites],
    }


def _load_site_policies(
    paths: Sequence[Path | str], ledger_entry: Mapping[str, Any]
) -> tuple[_policy.DcgSitePolicy, ...]:
    """Load explicit private input before any write planning.

    Once a home has adopted a site policy, omission is a hard error. A changed
    digest for the same site id is an ordinary policy update; silently changing
    the owning site identity is not.
    """
    recorded = ledger_entry.get("site_policy") or {}
    required = bool(recorded.get("required")) if isinstance(recorded, Mapping) else False
    if required and not paths:
        raise ValidationError(
            DCG_RECONCILE_SITE_POLICY_REQUIRED,
            "This managed home requires its private DCG site policy input.",
            next_actions=[
                "Re-run with the same `--site-policy` source used for adoption."
            ],
            recoverable=False,
        )
    sites = tuple(_policy.load_site_policy_file(Path(path)) for path in paths)
    if required:
        prior_sites = recorded.get("sites") or []
        prior_ids = [
            str(item.get("site_id") or "")
            for item in prior_sites
            if isinstance(item, Mapping)
        ]
        current_ids = [site.site_id for site in sites]
        if prior_ids != current_ids:
            raise ValidationError(
                DCG_RECONCILE_SITE_POLICY_REQUIRED,
                "The private DCG site-policy identity differs from the adopted owner.",
                context={"expected_site_ids": prior_ids, "actual_site_ids": current_ids},
                next_actions=[
                    "Restore the adopted site-policy source; use a reviewed migration for owner changes."
                ],
                recoverable=False,
            )
    return sites


def _semantic_pattern(pattern: str) -> str:
    """Collapse the one harmless quote escape used by the pre-owner live TOML."""
    return pattern.replace(r'\"', '"')


def _adoption_mismatches(existing_text: str, desired_text: str) -> list[str]:
    """Return policy sections that a generated takeover would weaken or alter."""
    try:
        existing = tomllib.loads(existing_text)
        desired = tomllib.loads(desired_text)
    except tomllib.TOMLDecodeError:
        return ["toml"]
    allowed_paths = _policy.UPSTREAM_KEY_PATHS
    if not _policy.key_paths(existing).issubset(allowed_paths):
        return ["unknown-keys"]

    mismatches: list[str] = []
    if existing.get("general") != desired.get("general"):
        mismatches.append("general")

    existing_packs = list((existing.get("packs") or {}).get("enabled") or [])
    desired_packs = list((desired.get("packs") or {}).get("enabled") or [])
    missing_packs = [pack for pack in existing_packs if pack not in desired_packs]
    added_packs = [pack for pack in desired_packs if pack not in existing_packs]
    if missing_packs or any(pack not in _policy.DEFAULT_PACKS for pack in added_packs):
        mismatches.append("packs")

    existing_overrides = existing.get("overrides") or {}
    desired_overrides = desired.get("overrides") or {}
    if list(existing_overrides.get("allow") or []) != list(desired_overrides.get("allow") or []):
        mismatches.append("allowlist")

    def blocks(document: Mapping[str, Any]) -> list[tuple[str, str]]:
        raw = (document.get("overrides") or {}).get("block") or []
        return [
            (_semantic_pattern(str(item.get("pattern") or "")), str(item.get("reason") or ""))
            for item in raw
            if isinstance(item, Mapping)
        ]

    if blocks(existing) != blocks(desired):
        mismatches.append("blocklist")
    if existing.get("agents") != desired.get("agents"):
        mismatches.append("agents")
    return mismatches


def _validate_marker_owned_policy(text: str) -> None:
    """Validate stale owned bytes without requiring the previous private source.

    The ledger intentionally stores no private rule bodies. Therefore a valid
    site-policy update can only prove the old render structurally: pinned keys,
    fail-closed type, canonical public block prefix, and no duplicate patterns.
    The new desired composition is validated exactly before it replaces this.
    """
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise _malformed(Path(POLICY_RELPATH), f"policy is not valid TOML: {exc}") from exc
    if not _policy.key_paths(document).issubset(_policy.UPSTREAM_KEY_PATHS):
        raise ValidationError(
            DCG_RECONCILE_MALFORMED_CONFIG,
            "Marker-owned DCG policy contains keys outside the pinned schema.",
            recoverable=False,
        )
    if not isinstance((document.get("general") or {}).get("fail_closed"), bool):
        raise ValidationError(
            DCG_RECONCILE_MALFORMED_CONFIG,
            "Marker-owned DCG policy lacks boolean fail_closed.",
            recoverable=False,
        )
    raw_blocks = (document.get("overrides") or {}).get("block") or []
    if not isinstance(raw_blocks, list):
        raise ValidationError(
            DCG_RECONCILE_MALFORMED_CONFIG,
            "Marker-owned DCG policy blocklist is not an array.",
            recoverable=False,
        )
    blocks = [
        (
            _semantic_pattern(str(item.get("pattern") or "")),
            str(item.get("reason") or ""),
        )
        for item in raw_blocks
        if isinstance(item, Mapping)
    ]
    public_prefix = [
        (_semantic_pattern(rule.pattern), rule.reason)
        for rule in _policy.DEFAULT_BLOCK_RULES
    ]
    if len(blocks) != len(raw_blocks) or blocks[: len(public_prefix)] != public_prefix:
        raise ValidationError(
            DCG_RECONCILE_MALFORMED_CONFIG,
            "Marker-owned DCG policy is missing or changes the public block-rule prefix.",
            recoverable=False,
        )
    patterns = [pattern for pattern, _reason in blocks]
    if len(patterns) != len(set(patterns)):
        raise ValidationError(
            DCG_RECONCILE_MALFORMED_CONFIG,
            "Marker-owned DCG policy contains duplicate block patterns.",
            recoverable=False,
        )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class _AgentPlan:
    agent: str
    path: Path
    state: str
    detail: str
    write: _Write | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    ledger: dict[str, Any] = field(default_factory=dict)
    before: Any = None
    after: Any = None
    grok: bool = False


def _plan_hook_file(
    *,
    agent: str,
    path: Path,
    binary: Path,
    grok: bool,
    ledger_entry: Mapping[str, Any],
    owned_commands: frozenset[str] = frozenset(),
) -> _AgentPlan:
    parsed = _load_json_file(path)
    before_document = parsed.document if parsed.exists else None
    base = before_document if before_document is not None else {}
    try:
        merged = _converge_document(base, binary=binary, grok=grok, owned_commands=owned_commands)
    except _Unsupported as exc:
        return _AgentPlan(
            agent=agent,
            path=path,
            state=STATE_UNSUPPORTED,
            detail=f"left untouched: {exc.detail}",
            before=before_document,
            after=before_document,
            ledger=dict(ledger_entry),
            grok=grok,
        )

    data = _dump_json(
        merged.document,
        indent=parsed.indent,
        trailing_newline=parsed.trailing_newline,
    )
    write = _plan_write(path, data)
    ledger_record = {
        "created_file": bool(ledger_entry.get("created_file")) or not parsed.exists,
        "created_hooks": bool(ledger_entry.get("created_hooks")) or merged.created_hooks,
        "created_event": bool(ledger_entry.get("created_event")) or merged.created_event,
    }
    if write.is_noop:
        return _AgentPlan(
            agent=agent,
            path=path,
            state=STATE_HEALTHY,
            detail="dcg hook already converged",
            write=None,
            extra={"adopted": merged.adopted, "duplicates_removed": 0},
            ledger=ledger_record,
            before=before_document,
            after=merged.document,
            grok=grok,
        )
    return _AgentPlan(
        agent=agent,
        path=path,
        state=STATE_CHANGED,
        detail=(
            "created hook config"
            if not parsed.exists
            else (
                f"collapsed {merged.duplicates_removed} duplicate dcg hook(s)"
                if merged.duplicates_removed
                else ("updated stale dcg hook" if merged.adopted else "added dcg hook")
            )
        ),
        write=write,
        extra={"adopted": merged.adopted, "duplicates_removed": merged.duplicates_removed},
        ledger=ledger_record,
        before=before_document,
        after=merged.document,
        grok=grok,
    )


def _plan_policy(
    layout_: Layout,
    ledger_entry: Mapping[str, Any],
    *,
    desired_policy: _policy.DcgPolicy,
    sites: Sequence[_policy.DcgSitePolicy],
    adopt_policy: bool,
) -> _AgentPlan:
    path = layout_.policy_config
    raw = _read_text(path)
    desired = _policy_text(desired_policy)
    site_receipt = _site_policy_receipt(sites)
    if raw is not None and not raw.startswith(_policy.GENERATED_MARKER):
        if adopt_policy:
            mismatches = _adoption_mismatches(raw, desired)
            if mismatches:
                raise ValidationError(
                    DCG_RECONCILE_POLICY_ADOPTION_MISMATCH,
                    "Generated DCG policy adoption would change or drop hand-owned behavior.",
                    context={"mismatched_sections": mismatches},
                    next_actions=[
                        "Repair the private site policy until the semantic before/after audit is lossless."
                    ],
                    recoverable=False,
                )
        else:
            return _AgentPlan(
                agent="policy",
                path=path,
                state=STATE_NEEDS_OPERATOR,
                detail="user DCG config is hand-owned (no generated marker); left untouched",
                extra={
                    "site_policy": site_receipt,
                    "operator_action": (
                        "Review the policy and re-run with `--adopt-policy` only after "
                        "the private site source preserves every existing section."
                    ),
                },
                ledger=dict(ledger_entry),
            )
    if raw is not None:
        # Ours: it must still parse and satisfy every policy invariant.
        _load_toml_file(path)
        if raw.startswith(_policy.GENERATED_MARKER):
            _validate_marker_owned_policy(raw)
    write = _plan_write(path, desired.encode("utf-8"))
    ledger_record = {
        "created_file": bool(ledger_entry.get("created_file")) or raw is None,
        "marker": _policy.GENERATED_MARKER,
        "site_policy": site_receipt,
    }
    if write.is_noop:
        return _AgentPlan(
            agent="policy",
            path=path,
            state=STATE_HEALTHY,
            detail=f"policy v{_policy.POLICY_VERSION} already rendered",
            extra={"site_policy": site_receipt},
            ledger=ledger_record,
        )
    return _AgentPlan(
        agent="policy",
        path=path,
        state=STATE_CHANGED,
        detail=(
            "rendered user policy"
            if raw is None
            else ("adopted lossless hand-owned policy" if adopt_policy and not raw.startswith(_policy.GENERATED_MARKER) else "re-rendered stale user policy")
        ),
        write=write,
        extra={"site_policy": site_receipt},
        ledger=ledger_record,
    )


def _plan_binary_link(layout_: Layout, ledger_entry: Mapping[str, Any]) -> dict[str, Any]:
    """Keep ``~/.local/bin/dcg`` pointing at the pinned binary, if it is elsewhere."""
    link = layout_.default_binary
    record: dict[str, Any] = {
        "path": str(link),
        "state": STATE_HEALTHY,
        "detail": "binary is at the managed default path",
        "created": bool(ledger_entry.get("created")),
        "action": "none",
    }
    if layout_.binary == link:
        return record
    if not layout_.binary.is_file():
        record["state"] = STATE_NEEDS_OPERATOR
        record["detail"] = "cannot link a binary that is not installed"
        return record
    if link.is_symlink():
        if Path(os.readlink(link)) == layout_.binary:
            record["detail"] = f"symlink -> {layout_.binary}"
            return record
        record["action"] = "relink"
        record["state"] = STATE_CHANGED
        record["detail"] = f"symlink retargeted -> {layout_.binary}"
        return record
    if link.exists():
        record["state"] = STATE_NEEDS_OPERATOR
        record["detail"] = f"{link} exists and is not a Skillbox symlink; left untouched"
        return record
    record["action"] = "link"
    record["state"] = STATE_CHANGED
    record["detail"] = f"symlink created -> {layout_.binary}"
    return record


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _owned_commands(layout_: Layout, ledger: Mapping[str, Any]) -> frozenset[str]:
    """Command strings this module is allowed to claim, beyond the ``dcg`` shape."""
    commands = {str(layout_.binary), str(layout_.default_binary)}
    recorded = str(ledger.get("binary") or "").strip()
    if recorded:
        commands.add(recorded)
    return frozenset(commands)


def _worst_status(states: Iterable[str]) -> str:
    order = [STATE_FAILED, STATE_UNSUPPORTED, STATE_NEEDS_OPERATOR]
    seen = set(states)
    for state in order:
        if state in seen:
            return state
    return STATE_HEALTHY


def _state_digest(payload: Mapping[str, Any]) -> str:
    return "sha256:" + _sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _pin_or_unsupported(platform: str | None) -> tuple[Any, dict[str, Any] | None]:
    system = machine = None
    if platform:
        system, _, machine = platform.partition("/")
    try:
        return _dist.asset_pin(system, machine), None
    except _dist.UnsupportedPlatformError as exc:
        return None, exc.to_payload()


def _base_payload(action: str, layout_: Layout) -> dict[str, Any]:
    return {
        "schema_version": RECONCILE_SCHEMA_VERSION,
        "action": action,
        "home": str(layout_.home),
        "binary": str(layout_.binary),
        "dcg_version": _dist.DCG_VERSION,
        "bypass_flag_used": False,
    }


def _apply_or_verify(
    home: Path | str,
    *,
    binary: Path | str | None = None,
    site_policy_paths: Sequence[Path | str] = (),
    adopt_policy: bool = False,
    dry_run: bool = False,
    platform: str | None = None,
    action: str = "apply",
) -> dict[str, Any]:
    layout_ = layout(home, binary)
    payload = _base_payload(action, layout_)
    payload["dry_run"] = bool(dry_run) or action == "verify"

    pin, unsupported = _pin_or_unsupported(platform)
    if pin is None:
        payload.update(
            {
                "result": RESULT_UNCHANGED,
                "status": STATE_UNSUPPORTED,
                "agents": [],
                "changed": [],
                "unrelated_preserved": True,
                "operator_actions": [],
                "unsupported": unsupported,
                "state_digest": _state_digest({"unsupported": True}),
            }
        )
        return payload

    ledger = _load_ledger(layout_.ledger)
    ledger_agents = ledger.get("agents") if isinstance(ledger.get("agents"), dict) else {}
    ledger_policy = ledger.get("policy") if isinstance(ledger.get("policy"), dict) else {}
    sites = _load_site_policies(site_policy_paths, ledger_policy)
    desired_policy = _policy.build_policy(site_policies=sites)
    desired_text = _policy_text(desired_policy)
    _policy.validate_rendered(desired_text, expected_policy=desired_policy)
    # The binary this home was last converged against is still ours to adopt,
    # even after the pinned path moves.
    owned_commands = _owned_commands(layout_, ledger)

    plans = [
        _plan_hook_file(
            agent="claude",
            path=layout_.claude_settings,
            binary=layout_.binary,
            grok=False,
            ledger_entry=ledger_agents.get("claude") or {},
            owned_commands=owned_commands,
        ),
        _plan_hook_file(
            agent="codex",
            path=layout_.codex_hooks,
            binary=layout_.binary,
            grok=False,
            ledger_entry=ledger_agents.get("codex") or {},
            owned_commands=owned_commands,
        ),
        _plan_hook_file(
            agent="grok",
            path=layout_.grok_hook,
            binary=layout_.binary,
            grok=True,
            ledger_entry=ledger_agents.get("grok") or {},
            owned_commands=owned_commands,
        ),
    ]
    policy_plan = _plan_policy(
        layout_,
        ledger_policy,
        desired_policy=desired_policy,
        sites=sites,
        adopt_policy=adopt_policy,
    )
    binary_info = _binary_report(layout_, pin)
    link_info = _plan_binary_link(layout_, ledger.get("binary_link") or {})

    writes = [plan.write for plan in [*plans, policy_plan] if plan.write is not None]
    mutating = action == "apply" and not dry_run

    codex_plan = next(plan for plan in plans if plan.agent == "codex")
    codex_mutated = codex_plan.write is not None
    codex_after = codex_plan.after if codex_plan.after is not None else codex_plan.before
    codex_bytes = (
        codex_plan.write.data
        if codex_plan.write is not None
        else (_file_bytes(layout_.codex_hooks) or b"")
    )
    observed = _codex_trusted_hash(layout_, codex_after)
    trust_state, trust_record = _codex_trust_state(
        ledger_trust=ledger.get("codex_trust") or {},
        observed=observed,
        digest=_sha256_bytes(codex_bytes),
        # Only a run that actually rewrites hooks.json invalidates trust; verify
        # and --dry-run observe, they never move the trust ledger.
        mutated=codex_mutated and mutating,
    )
    codex_plan.extra["trust"] = {
        "state": trust_state,
        "source": str(layout_.codex_config),
        "persisted_hash": observed,
        "hooks_sha256": trust_record["hooks_sha256"],
        "bypass_flag_forbidden": BYPASS_FLAG,
    }
    if trust_state != CODEX_TRUST_TRUSTED:
        codex_plan.extra["trust"]["operator_action"] = CODEX_TRUST_ACTION

    # Health, as opposed to mutation: a converged Codex hook is NOT healthy until
    # Codex has persisted its own trust hash for exactly these bytes.
    def _health_of(state: str) -> str:
        """A drift this run actually repaired is healthy; an unrepaired one is not."""
        if state != STATE_CHANGED:
            return state
        return STATE_HEALTHY if mutating else STATE_NEEDS_OPERATOR

    agent_reports: list[dict[str, Any]] = []
    for plan in plans:
        state = plan.state
        if not mutating and state == STATE_CHANGED:
            plan.detail = f"drift: {plan.detail} (run apply)"
        health = _health_of(state)
        if plan.agent == "codex" and trust_state != CODEX_TRUST_TRUSTED and health == STATE_HEALTHY:
            health = STATE_NEEDS_OPERATOR
        report = {
            "agent": plan.agent,
            "path": str(plan.path),
            "state": state,
            "health": health,
            "detail": plan.detail,
        }
        report.update(plan.extra)
        agent_reports.append(report)

    policy_state = policy_plan.state
    if not mutating and policy_state == STATE_CHANGED:
        policy_plan.detail = f"drift: {policy_plan.detail} (run apply)"
    policy_report = {
        "agent": "policy",
        "path": str(policy_plan.path),
        "state": policy_state,
        "health": _health_of(policy_state),
        "detail": policy_plan.detail,
    }
    policy_report.update(policy_plan.extra)

    health_states = [
        binary_info["state"],
        _health_of(link_info["state"]),
        policy_report["health"],
        *[report["health"] for report in agent_reports],
    ]

    operator_actions: list[str] = []
    for source in (binary_info, link_info, policy_report, *agent_reports):
        action_text = source.get("operator_action")
        if action_text:
            operator_actions.append(str(action_text))
        trust = source.get("trust")
        if isinstance(trust, Mapping) and trust.get("operator_action"):
            operator_actions.append(str(trust["operator_action"]))

    # Ledger + link mutations join the same atomic batch.
    ledger_next = json.loads(json.dumps(ledger))
    ledger_next["schema_version"] = RECONCILE_SCHEMA_VERSION
    ledger_next["binary"] = str(layout_.binary)
    ledger_next.setdefault("agents", {})
    for plan in plans:
        if plan.ledger:
            ledger_next["agents"][plan.agent] = {
                key: value for key, value in plan.ledger.items() if key != "path"
            }
    ledger_next["policy"] = policy_plan.ledger or ledger.get("policy") or {}
    ledger_next["codex_trust"] = trust_record
    ledger_next["binary_link"] = {
        "path": str(layout_.default_binary),
        "created": bool(link_info.get("created")) or link_info["action"] == "link",
    }

    changed_paths = [str(write.path) for write in writes]
    if link_info["action"] in {"link", "relink"}:
        changed_paths.append(str(layout_.default_binary))

    backup_id = ""
    if mutating and writes:
        backups = ledger_next.get("backups") or {"next_id": 1, "last": ""}
        backup_id = f"{int(backups.get('next_id', 1)):04d}"
        ledger_next["backups"] = {"next_id": int(backups.get("next_id", 1)) + 1, "last": backup_id}

    ledger_write = _plan_write(layout_.ledger, _ledger_bytes(ledger_next))

    if mutating:
        batch = list(writes)
        if not ledger_write.is_noop:
            batch.append(ledger_write)
        if backup_id and batch:
            _write_backup(layout_, batch, backup_id)
        if batch:
            _commit(batch)
        if link_info["action"] in {"link", "relink"}:
            layout_.default_binary.parent.mkdir(parents=True, exist_ok=True)
            if layout_.default_binary.is_symlink():
                layout_.default_binary.unlink()
            layout_.default_binary.symlink_to(layout_.binary)

    unrelated_preserved = all(
        _unrelated_view(plan.before, grok=plan.grok, owned_commands=owned_commands)
        == _unrelated_view(plan.after, grok=plan.grok, owned_commands=owned_commands)
        for plan in plans
    )

    payload.update(
        {
            "result": RESULT_CHANGED if (writes and action == "apply") else RESULT_UNCHANGED,
            "status": _worst_status(health_states),
            "binary_state": binary_info,
            "binary_link": link_info,
            "policy": policy_report,
            "agents": agent_reports,
            "changed": changed_paths if action == "apply" else [],
            "pending_changes": changed_paths if action == "verify" else [],
            "backup": backup_id,
            "unrelated_preserved": unrelated_preserved,
            "operator_actions": sorted(set(operator_actions)),
            "codex_trust": trust_state,
        }
    )
    payload["state_digest"] = _state_digest(
        {
            "version": _dist.DCG_VERSION,
            "command": str(layout_.binary),
            "claude": _sha256_bytes(_file_bytes(layout_.claude_settings) or b""),
            "codex": _sha256_bytes(_file_bytes(layout_.codex_hooks) or b""),
            "grok": _sha256_bytes(_file_bytes(layout_.grok_hook) or b""),
            "policy": _sha256_bytes(_file_bytes(layout_.policy_config) or b""),
        }
    )
    return payload


def apply(
    home: Path | str,
    *,
    binary: Path | str | None = None,
    site_policy_paths: Sequence[Path | str] = (),
    adopt_policy: bool = False,
    dry_run: bool = False,
    platform: str | None = None,
) -> dict[str, Any]:
    """Converge a managed home. Atomic, merge-safe, idempotent."""
    return _apply_or_verify(
        home,
        binary=binary,
        site_policy_paths=site_policy_paths,
        adopt_policy=adopt_policy,
        dry_run=dry_run,
        platform=platform,
        action="apply",
    )


def verify(
    home: Path | str,
    *,
    binary: Path | str | None = None,
    site_policy_paths: Sequence[Path | str] = (),
    adopt_policy: bool = False,
    platform: str | None = None,
) -> dict[str, Any]:
    """Report convergence and health without writing anything."""
    return _apply_or_verify(
        home,
        binary=binary,
        site_policy_paths=site_policy_paths,
        adopt_policy=adopt_policy,
        dry_run=True,
        platform=platform,
        action="verify",
    )


def relinquish(
    home: Path | str,
    *,
    binary: Path | str | None = None,
    dry_run: bool = False,
    purge: bool = False,
) -> dict[str, Any]:
    """Remove ONLY DCG-owned hook entries, links, and marker-stamped policy.

    The reconcile ledger is trimmed to its backup pointer (and the backup set is
    kept) so ``rollback`` still works after a relinquish; ``purge=True`` drops
    that recovery data too and leaves the home with no DCG state at all.
    """
    layout_ = layout(home, binary)
    payload = _base_payload("relinquish", layout_)
    payload["dry_run"] = bool(dry_run)

    ledger = _load_ledger(layout_.ledger)
    ledger_agents = ledger.get("agents") if isinstance(ledger.get("agents"), dict) else {}
    owned_commands = _owned_commands(layout_, ledger)

    writes: list[_Write] = []
    reports: list[dict[str, Any]] = []
    removed_entries = 0
    preserved = True

    for agent, path, grok in (
        ("claude", layout_.claude_settings, False),
        ("codex", layout_.codex_hooks, False),
        ("grok", layout_.grok_hook, True),
    ):
        entry = ledger_agents.get(agent) or {}
        parsed = _load_json_file(path)
        if not parsed.exists:
            reports.append(
                {"agent": agent, "path": str(path), "state": STATE_HEALTHY, "detail": "no config present"}
            )
            continue
        try:
            stripped, changed, removed = _strip_owned(
                parsed.document,
                created_hooks=bool(entry.get("created_hooks")),
                created_event=bool(entry.get("created_event")),
                owned_commands=owned_commands,
            )
        except _Unsupported as exc:
            reports.append(
                {
                    "agent": agent,
                    "path": str(path),
                    "state": STATE_UNSUPPORTED,
                    "detail": f"left untouched: {exc.detail}",
                }
            )
            continue

        preserved = preserved and _unrelated_view(
            parsed.document, grok=grok, owned_commands=owned_commands
        ) == _unrelated_view(stripped, grok=grok, owned_commands=owned_commands)
        removed_entries += removed

        delete_file = bool(entry.get("created_file")) and _is_empty_document(stripped)
        if delete_file:
            write = _plan_write(path, None)
            detail = f"removed {removed} dcg hook(s) and the file this module created"
        else:
            data = _dump_json(stripped, indent=parsed.indent, trailing_newline=parsed.trailing_newline)
            write = _plan_write(path, data)
            detail = (
                f"removed {removed} dcg hook(s); unrelated entries kept"
                if removed
                else "no dcg hook present"
            )
        if write.is_noop or not changed:
            reports.append(
                {"agent": agent, "path": str(path), "state": STATE_HEALTHY, "detail": "no dcg hook present"}
            )
            continue
        writes.append(write)
        reports.append(
            {
                "agent": agent,
                "path": str(path),
                "state": STATE_CHANGED,
                "detail": detail,
                "removed": removed,
            }
        )

    # Policy: only ever remove a file carrying the generated marker.
    policy_raw = _read_text(layout_.policy_config)
    if policy_raw is None:
        policy_report = {"agent": "policy", "path": str(layout_.policy_config), "state": STATE_HEALTHY, "detail": "absent"}
    elif not policy_raw.startswith(_policy.GENERATED_MARKER):
        policy_report = {
            "agent": "policy",
            "path": str(layout_.policy_config),
            "state": STATE_HEALTHY,
            "detail": "hand-owned config preserved (no generated marker)",
        }
    else:
        writes.append(_plan_write(layout_.policy_config, None))
        policy_report = {
            "agent": "policy",
            "path": str(layout_.policy_config),
            "state": STATE_CHANGED,
            "detail": "removed marker-stamped policy render",
        }

    link_removed = False
    link_entry = ledger.get("binary_link") or {}
    if bool(link_entry.get("created")) and layout_.default_binary.is_symlink():
        link_removed = True

    removed_anything = bool(writes) or link_removed
    backup_id = ""
    if not dry_run:
        if writes:
            backups = ledger.get("backups") or {"next_id": 1, "last": ""}
            backup_id = f"{int(backups.get('next_id', 1)):04d}"
            trimmed = {
                "schema_version": RECONCILE_SCHEMA_VERSION,
                "agents": {},
                "policy": {},
                "binary_link": {},
                "codex_trust": {},
                "backups": {"next_id": int(backups.get("next_id", 1)) + 1, "last": backup_id},
            }
            batch = list(writes)
            ledger_write = _plan_write(layout_.ledger, _ledger_bytes(trimmed))
            if not ledger_write.is_noop:
                batch.append(ledger_write)
            _write_backup(layout_, batch, backup_id)
            _commit(batch)
        if link_removed:
            layout_.default_binary.unlink()
        if purge:
            if layout_.backups_root.is_dir():
                shutil.rmtree(layout_.backups_root)
            if layout_.ledger.exists():
                layout_.ledger.unlink()
        _prune_empty_dirs(layout_)

    payload.update(
        {
            "result": RESULT_REMOVED if removed_anything else RESULT_UNCHANGED,
            "status": STATE_HEALTHY,
            "agents": reports,
            "policy": policy_report,
            "binary_link_removed": link_removed,
            "removed_entries": removed_entries,
            "unrelated_preserved": preserved,
            "changed": [str(write.path) for write in writes],
            "backup": backup_id,
            "purged": bool(purge),
            "operator_actions": [],
        }
    )
    return payload


def _is_empty_document(document: Any) -> bool:
    if not isinstance(document, dict):
        return False
    leftovers = {key: value for key, value in document.items() if key != "description"}
    if leftovers:
        return False
    return document.get("description") in (None, GROK_DESCRIPTION)


def _prune_empty_dirs(layout_: Layout) -> None:
    for directory in (
        layout_.grok_hook.parent,
        layout_.grok_hook.parent.parent,
        layout_.codex_hooks.parent,
        layout_.backups_root,
        layout_.policy_config.parent,
        layout_.policy_config.parent.parent,
    ):
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass


def rollback(home: Path | str, *, binary: Path | str | None = None) -> dict[str, Any]:
    """Restore the home to the bytes recorded by the last mutating run."""
    layout_ = layout(home, binary)
    payload = _base_payload("rollback", layout_)
    ledger = _load_ledger(layout_.ledger)
    backup_id = str((ledger.get("backups") or {}).get("last") or "")
    backup_dir = layout_.backups_root / backup_id if backup_id else None
    manifest_path = (backup_dir / "manifest.json") if backup_dir else None
    if not backup_id or manifest_path is None or not manifest_path.is_file():
        raise SkillboxError(
            DCG_RECONCILE_NO_BACKUP,
            f"No DCG reconcile backup recorded for {layout_.home}; nothing to roll back.",
            context={"home": str(layout_.home), "backups_root": str(layout_.backups_root)},
            next_actions=["Run `apply` first; every mutating run records a restorable backup set."],
            recoverable=True,
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    restored: list[str] = []
    for entry in manifest.get("files") or []:
        target = layout_.home / str(entry["path"])
        if entry.get("existed"):
            blob = backup_dir / "files" / str(entry.get("blob"))
            target.parent.mkdir(parents=True, exist_ok=True)
            data = blob.read_bytes()
            if entry.get("sha256") and _sha256_bytes(data) != entry["sha256"]:
                raise SkillboxError(
                    DCG_RECONCILE_WRITE_FAILED,
                    f"Backup blob for {target} is corrupt; refusing to restore.",
                    context={"path": str(target)},
                    recoverable=False,
                )
            target.write_bytes(data)
        elif target.exists() or target.is_symlink():
            target.unlink()
        restored.append(str(target))

    shutil.rmtree(backup_dir)
    _prune_empty_dirs(layout_)

    payload.update(
        {
            "result": RESULT_ROLLED_BACK,
            "status": STATE_HEALTHY,
            "backup": backup_id,
            "restored": restored,
            "changed": restored,
            "unrelated_preserved": True,
            "operator_actions": [],
            "agents": [],
        }
    )
    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


_TEXT_ORDER = ("binary_state", "binary_link", "policy")


def _text_lines(payload: Mapping[str, Any]) -> list[str]:
    lines = [
        f"action:  {payload.get('action')}",
        f"home:    {payload.get('home')}",
        f"result:  {payload.get('result')}",
        f"status:  {payload.get('status')}",
    ]
    for key in _TEXT_ORDER:
        section = payload.get(key)
        if isinstance(section, Mapping):
            lines.append(f"{key:<8} {section.get('state')}: {section.get('detail', '')}".rstrip())
    for agent in payload.get("agents") or []:
        if isinstance(agent, Mapping):
            lines.append(f"{str(agent.get('agent')):<8} {agent.get('state')}: {agent.get('detail', '')}".rstrip())
    if payload.get("codex_trust"):
        lines.append(f"codex trust: {payload['codex_trust']}")
    for action_text in payload.get("operator_actions") or []:
        lines.append(f"operator: {action_text}")
    return lines


def _exit_code(payload: Mapping[str, Any]) -> int:
    status = payload.get("status")
    if status == STATE_FAILED:
        return EXIT_FAILED
    if status == STATE_UNSUPPORTED:
        return EXIT_UNSUPPORTED
    if status == STATE_NEEDS_OPERATOR:
        return EXIT_NEEDS_OPERATOR
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """``python3 -m runtime_manager.dcg_reconcile <action> --home H``."""
    import argparse

    args_list = list(argv) if argv is not None else None
    if args_list is None:
        import sys as _sys

        args_list = _sys.argv[1:]

    if any(BYPASS_FLAG in str(arg) for arg in args_list):
        error = ValidationError(
            DCG_RECONCILE_BYPASS_FORBIDDEN,
            f"{BYPASS_FLAG} is forbidden as setup, proof, or remediation.",
            context={"flag": BYPASS_FLAG},
            next_actions=[CODEX_TRUST_ACTION],
            recoverable=False,
        )
        print(json.dumps(error.to_payload(), indent=2, sort_keys=True))
        return EXIT_FAILED

    parser = argparse.ArgumentParser(prog="dcg_reconcile")
    parser.add_argument("action", choices=("apply", "verify", "relinquish", "rollback"))
    parser.add_argument("--home", required=True, help="managed home to converge (never inferred)")
    parser.add_argument("--binary", default="", help="pinned dcg binary path")
    parser.add_argument(
        "--site-policy",
        action="append",
        default=[],
        metavar="PATH",
        help="private v1 site-policy JSON; repeatable and required after adoption",
    )
    parser.add_argument(
        "--adopt-policy",
        action="store_true",
        help="replace a hand-owned policy only after a lossless semantic audit",
    )
    parser.add_argument("--platform", default="", help="os/machine override")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--purge",
        action="store_true",
        help="relinquish only: also drop the reconcile ledger and backup sets",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(args_list)

    binary = args.binary or None
    try:
        if args.action == "apply":
            payload = apply(
                args.home,
                binary=binary,
                site_policy_paths=args.site_policy,
                adopt_policy=args.adopt_policy,
                dry_run=args.dry_run,
                platform=args.platform or None,
            )
        elif args.action == "verify":
            payload = verify(
                args.home,
                binary=binary,
                site_policy_paths=args.site_policy,
                adopt_policy=args.adopt_policy,
                platform=args.platform or None,
            )
        elif args.action == "relinquish":
            payload = relinquish(args.home, binary=binary, dry_run=args.dry_run, purge=args.purge)
        else:
            payload = rollback(args.home, binary=binary)
    except SkillboxError as exc:
        body = exc.to_payload()
        body["action"] = args.action
        body["result"] = RESULT_FAILED
        body["status"] = STATE_FAILED
        if args.format == "json":
            print(json.dumps(body, indent=2, sort_keys=True))
        else:
            print(f"action:  {args.action}")
            print(f"result:  {RESULT_FAILED}")
            print(f"status:  {STATE_FAILED}")
            print(f"error:   [{exc.code}] {exc.message}")
        return EXIT_FAILED

    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("\n".join(_text_lines(payload)))
    return _exit_code(payload)


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(main())
