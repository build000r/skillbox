"""``doctor --fix``: a gated, backed-up, undoable, evidence-producing mutation.

Pass-2 finding ``F-doc-10``: *no* doctor in the family had ``--fix``, a fix
preview, or a per-run artifact — every finding required a human to copy-paste a
``fix_command`` with no undo trail, and repeated doctor runs left no evidence
for a later session. ``make self-test`` was the only family member that wrote a
receipt.

This module is the shared engine behind every doctor's ``--fix``. It is
standard-library only and lives beside :mod:`doctor_contract` in ``scripts/lib``
so the outer reconcile script (which cannot import ``runtime_manager``) and the
inner runtime manager share ONE implementation rather than two that drift.

The five properties it guarantees
================================

1. **Nothing runs that was not declared.** ``--fix`` never executes a finding's
   ``fix_command`` string. It executes a :class:`FixSpec` from a per-doctor
   registry: a fixed ``argv`` (no shell), a description, and the exact paths to
   back up first. A finding with no registered spec is *skipped with a reason*,
   never guessed at. This is the difference between "the doctor repairs known
   drift" and "the doctor runs arbitrary strings it printed to itself".

2. **Dry-run by default, confirmation-gated.** ``--fix`` alone computes and
   records the plan, writes a run artifact, and exits
   :data:`~doctor_contract.EXIT_NEEDS_INPUT` (3) — the family slot whose
   published meaning is "operator input required". Only ``--fix --yes`` mutates.

3. **Pre-change backup.** Every path a spec declares is snapshotted into the run
   directory *before* the first command runs, including paths that do not exist
   yet (recorded as ``existed: false``, so undo removes what the fix created).

4. **A real undo — that treats its own artifact as untrusted input.**
   ``--undo <artifact>`` replays the backups in reverse. The exact invocation is
   written into the artifact AND printed, so the undo path is discoverable from
   the evidence alone with no memory of this session. Because an artifact is a
   JSON file on a container-writable bind mount, undo is NOT a trusting reader
   of it — see :func:`plan_undo`. Every property below is enforced before undo
   touches anything:

   * ``--undo`` alone is a PLAN (exit ``EXIT_NEEDS_INPUT``); ``--undo --yes``
     is the only thing that removes or restores a byte.
   * No ``expandvars`` anywhere, and no ``expanduser`` on artifact-supplied
     paths: undo acts on the absolute path the FIX resolved and recorded, never
     on a string it re-expands against the current environment.
   * The artifact must live inside this state root's run directory, name this
     repo root, and carry a valid HMAC over its own path list.
   * Every target must still resolve to exactly where the fix recorded it
     (ancestor symlinks resolved), and must sit strictly inside a recorded
     write-scope root. ``..`` and swapped symlinks fail closed.
   * A path is deleted ONLY if the fix created it from nothing AND its contents
     still match the post-fix manifest. A created directory that has since been
     populated (``sync`` filling ``repos/``) is REFUSED with a loud message —
     a partial undo with an explanation beats deleting somebody's work.

5. **Single-writer gating.** Mutation happens inside the repo's authoritative
   state-root lease (``runtime_manager.state_mutation.state_mutation_lease``)
   under a ``MANIFEST``-classified ``boundary_id``. If the lease cannot be
   reached the fix REFUSES — fail-closed. An ungated mutation is never the
   degrade path.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from lib.doctor_contract import (
    EXIT_DRIFT,
    EXIT_ERROR,
    EXIT_NEEDS_INPUT,
    EXIT_OK,
    STATUS_FAIL,
    STATUS_PASS,
    Finding,
    normalize_status,
)

#: A fixer that RAN and failed is a genuine error (the doctor did its job; the
#: remediation did not), so it exits 1 rather than 4 — 4 means "found drift",
#: which is exactly what an apply run was trying to clear.
EXIT_ERROR_ON_FIXER_FAILURE = EXIT_ERROR

#: Bumped whenever the run-artifact JSON gains/renames/drops a key. v2 added the
#: fields undo needs to be safe: per-entry ``resolved``/``scope``, the post-fix
#: ``created`` manifest, the artifact-level ``write_scope``, and ``integrity``.
#: A v1 artifact is refused rather than migrated — v1 is exactly the format
#: whose undo path could not be trusted.
RUN_ARTIFACT_SCHEMA_VERSION = "2026-08-14+doctor-run-artifact.v2"

#: Where run artifacts live, relative to the resolved state root.
RUNS_DIRNAME = "doctor-runs"

#: HMAC key binding an artifact to the engine that wrote it, so a hand-planted
#: JSON file fails closed. Created 0600 on first use beside the run directories.
#:
#: Honest threat model: this key is a file, so an attacker who can READ the
#: state root can forge a signature. It is the cheap outer gate, not the load-
#: bearing one — containment (``plan_undo``) is what makes a forged artifact
#: harmless, and containment does not depend on the key at all.
INTEGRITY_KEY_NAME = ".undo-signing-key"
INTEGRITY_ALGORITHM = "hmac-sha256"

#: The artifact fields the signature covers. Everything undo ACTS on is here.
SIGNED_FIELDS = (
    "run_id",
    "tool",
    "boundary_id",
    "repo_root",
    "state_root",
    "artifact_path",
    "backup_root",
    "write_scope",
    "backups",
)

#: Cap on the post-fix content manifest recorded for a created directory. Past
#: this, the manifest is marked truncated and undo REFUSES to delete rather than
#: deleting on incomplete evidence.
CREATED_MANIFEST_CAP = 2000

STATE_ROOT_ENV = "SKILLBOX_STATE_ROOT"
DEFAULT_STATE_ROOT_REL = ".skillbox-state"

MODE_PREVIEW = "preview"
MODE_APPLY = "apply"
MODE_UNDO = "undo"

#: How much of a fixer's output is retained in the artifact. Enough to diagnose,
#: bounded so a chatty fixer cannot turn the evidence file into a log dump.
OUTPUT_TAIL_CHARS = 4000


class DoctorFixError(RuntimeError):
    """A ``--fix`` run could not proceed. Always fail-closed, never a degrade."""


# --------------------------------------------------------------------------- #
# Declarative fixer registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FixSpec:
    """The ONLY thing ``--fix`` is allowed to execute for a given finding code.

    ``command`` is an argv list run WITHOUT a shell from the repo root.
    ``backup_paths`` are files or directories captured before the command runs
    — repo-relative, or ABSOLUTE for the surfaces that legitimately live outside
    the checkout (``~/.claude.json``, ``~/.codex/config.toml``). They are what
    ``--undo`` restores, so a spec that writes a path it did not declare is a
    bug the artifact will make visible (the undo simply will not cover it).
    """

    code: str
    command: tuple[str, ...]
    description: str
    backup_paths: tuple[str, ...] = ()
    timeout_s: float = 600.0

    @property
    def command_text(self) -> str:
        return " ".join(self.command)

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "command": list(self.command),
            "command_text": self.command_text,
            "description": self.description,
            "backup_paths": list(self.backup_paths),
        }


def build_registry(specs: Iterable[FixSpec]) -> dict[str, FixSpec]:
    return {spec.code: spec for spec in specs}


# --------------------------------------------------------------------------- #
# Finding annotation — a finding must SAY whether --fix can touch it
# --------------------------------------------------------------------------- #

#: Why a finding is not auto-fixable. These strings are part of the contract:
#: an agent reads ``fix_reason`` to decide whether to escalate to a human.
REASON_HEALTHY = "nothing to fix — this check passes"
REASON_NO_SPEC = "no registered auto-fix; apply fix_command by hand and re-run the doctor"
REASON_ADVISORY = "advisory only — no auto-fix is applied for a warning"


def annotate_fixable(
    findings: Sequence[Finding], registry: Mapping[str, FixSpec]
) -> list[Finding]:
    """Stamp ``fixable``/``fix_reason``/``fix_command`` from the registry.

    A passing check is never fixable. A ``warn`` is never auto-fixed either:
    warnings are advisory by the family's own definition, and a doctor that
    silently mutates on an advisory is a doctor agents stop trusting. Only
    ``fail`` and ``inco`` findings with a registered spec are fixable.
    """
    out: list[Finding] = []
    for finding in findings:
        status = normalize_status(finding.status)
        spec = registry.get(finding.code)
        if spec is not None and not finding.fix_command:
            finding.fix_command = spec.command_text
        if status == STATUS_PASS:
            finding.fixable = False
            # Deliberately blank: stamping "nothing to fix" on every passing
            # check would put the noise on the 90% of findings nobody reads.
            # REASON_HEALTHY is still the answer the run artifact records.
            finding.fix_reason = ""
        elif spec is None:
            finding.fixable = False
            finding.fix_reason = REASON_NO_SPEC
        elif status != STATUS_FAIL:
            finding.fixable = False
            finding.fix_reason = REASON_ADVISORY
        else:
            finding.fixable = True
            finding.fix_reason = ""
        out.append(finding)
    return out


# --------------------------------------------------------------------------- #
# State root + run directory
# --------------------------------------------------------------------------- #


def resolve_state_root(root_dir: Path) -> Path:
    """``$SKILLBOX_STATE_ROOT`` else ``<repo>/.skillbox-state``.

    Deliberately the REPO-relative reading of a relative override, matching
    ``scripts/self-test.sh:178`` and ``scripts/lib/opslib.py:235``; the lease
    refuses a relative root without an explicit base for exactly this reason, so
    we resolve it here and hand the lease an absolute path.
    """
    raw = str(os.environ.get(STATE_ROOT_ENV) or "").strip()
    if not raw:
        return (root_dir / DEFAULT_STATE_ROOT_REL).resolve()
    # expanduser only. `expandvars` is deliberately NOT used anywhere in this
    # module: it turns any path string into an environment-dependent one, which
    # is exactly the property that makes a re-expanded path unsafe to act on.
    expanded = Path(os.path.expanduser(raw))
    if not expanded.is_absolute():
        expanded = root_dir / expanded
    return expanded.resolve()


#: Short, stable directory names for the run artifacts. The `tool` field is the
#: command an agent types (`python3 .env-manager/manage.py doctor`), which makes
#: a terrible directory name; these are the names on disk. Keyed by tool so the
#: mapping cannot drift from lib/doctor_contract.FAMILY.
RUN_SLUGS: dict[str, str] = {
    "sbp doctor": "structure-doctor",
    "make doctor": "reconcile-doctor",
    "python3 .env-manager/manage.py doctor": "runtime-doctor",
}


def _slug(tool: str) -> str:
    known = RUN_SLUGS.get(tool)
    if known:
        return known
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in tool)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "doctor"


def runs_dir(root_dir: Path, tool: str) -> Path:
    return resolve_state_root(root_dir) / RUNS_DIRNAME / _slug(tool)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# The mutation gate — fail-closed
# --------------------------------------------------------------------------- #


def _load_state_mutation(root_dir: Path):
    """Import the authoritative lease module, or refuse.

    ``runtime_manager.state_mutation`` is stdlib-only and imports nothing else
    from its package at module scope (its own module docstring commits to that),
    and ``runtime_manager/__init__`` is a lazy PEP-562 facade, so this import is
    cheap even from the outer reconcile script, which otherwise never touches
    ``runtime_manager``.
    """
    env_manager = root_dir / ".env-manager"
    if str(env_manager) not in sys.path:
        sys.path.insert(0, str(env_manager))
    try:
        from runtime_manager.state_mutation import state_mutation_lease  # noqa: PLC0415

        return state_mutation_lease
    except Exception as exc:  # noqa: BLE001 — refusing is the only safe answer
        raise DoctorFixError(
            "the state-root mutation lease is unreachable "
            f"({type(exc).__name__}: {exc}); refusing to mutate ungated"
        ) from exc


@contextmanager
def mutation_gate(
    root_dir: Path, boundary_id: str, *, annotations: Mapping[str, Any] | None = None
) -> Iterator[Any]:
    """Hold the single-writer state-root lease for ``boundary_id``.

    ``boundary_id`` MUST be classified as a mutation in
    ``state_mutation.MANIFEST``; the lease rejects anything else, which is how
    a new ``--fix`` surface cannot ship without an inventory row.
    """
    lease = _load_state_mutation(root_dir)
    state_root = resolve_state_root(root_dir)
    state_root.mkdir(parents=True, exist_ok=True)
    with lease(state_root, boundary_id, annotations=dict(annotations or {})) as held:
        yield held


# --------------------------------------------------------------------------- #
# Backups
# --------------------------------------------------------------------------- #


@dataclass
class BackupEntry:
    path: str
    backup: str | None
    existed: bool
    kind: str  # file | dir | absent
    #: The absolute path the FIX resolved this declared path to, with every
    #: ancestor symlink already followed. Undo acts on THIS, never on a re-
    #: expansion of ``path`` — and refuses if re-resolving no longer agrees.
    resolved: str = ""
    #: Which recorded write-scope root ``resolved`` sits inside.
    scope: str = ""
    #: Post-fix evidence, only for a path the fix created from nothing:
    #: ``{"kind": "dir"|"file"|"symlink"|"absent", ...}``. Undo deletes only
    #: when the path still matches this exactly.
    created: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "backup": self.backup,
            "existed": self.existed,
            "kind": self.kind,
            "resolved": self.resolved,
            "scope": self.scope,
            "created": self.created,
        }


def _declared_abs(root_dir: Path, raw: str) -> Path:
    """Absolutize a SPEC-declared path (trusted: it is written in this repo).

    ``expanduser`` is allowed here because the surfaces that legitimately live
    outside the checkout are declared as ``~/.claude.json`` in Python source we
    own. ``expandvars`` is not used: see :data:`INTEGRITY_KEY_NAME`'s neighbours.
    """
    expanded = Path(str(raw)).expanduser()
    return expanded if expanded.is_absolute() else (root_dir / expanded)


def resolve_no_follow(path: Path) -> Path:
    """Absolute path with every ANCESTOR symlink resolved, leaf untouched.

    Two properties matter and ``Path.resolve()`` gives neither:

    * a symlinked *ancestor* is resolved, so ``a -> /etc`` in the middle of a
      declared path cannot smuggle a target out of its scope unnoticed;
    * the *leaf* is never followed, so a symlink the fix created is deleted as
      a link rather than punching through to whatever it points at.

    Non-existent ancestors are kept literally, which is what makes this usable
    on a path the fix has not created yet.
    """
    path = Path(path)
    parent = path.parent
    trailing: list[str] = []
    while not os.path.lexists(parent) and parent != parent.parent:
        trailing.append(parent.name)
        parent = parent.parent
    base = Path(os.path.realpath(str(parent)))
    for name in reversed(trailing):
        base = base / name
    return base / path.name if path.name else base


def _strictly_inside(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` is under ``root`` AND is not ``root`` itself."""
    if candidate == root:
        return False
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def write_scope_roots(root_dir: Path, resolved: Iterable[Path]) -> list[Path]:
    """The directories this boundary is allowed to write, recorded at fix time.

    The repo and the state root always qualify. ``$HOME`` qualifies only when a
    spec actually declares a path under it (the MCP client surfaces), so a run
    that never touches home never records home as writable. A declared path that
    lands outside all three is a BUG IN THE SPEC and refuses the fix outright —
    better to fail while nothing has changed than to record an unbounded scope.
    """
    scopes = [root_dir.resolve(), resolve_state_root(root_dir)]
    try:
        home = Path(os.path.realpath(str(Path.home())))
    except (OSError, RuntimeError):
        home = None
    for candidate in resolved:
        if any(_strictly_inside(scope, candidate) for scope in scopes):
            continue
        if home is not None and _strictly_inside(home, candidate) and home not in scopes:
            scopes.append(home)
            continue
        if not any(_strictly_inside(scope, candidate) for scope in scopes):
            raise DoctorFixError(
                f"fix spec declares {candidate}, which is outside every write scope "
                f"({', '.join(str(scope) for scope in scopes)}); refusing to run a fix "
                "whose undo could not be contained"
            )
    return scopes


def _scope_for(scopes: Sequence[Path], candidate: Path) -> Path | None:
    """The most specific recorded scope containing ``candidate``."""
    matches = [scope for scope in scopes if _strictly_inside(scope, candidate)]
    if not matches:
        return None
    return max(matches, key=lambda scope: len(scope.parts))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_created(path: Path) -> dict[str, Any]:
    """Record what the fix left at ``path``, precisely enough to re-verify it.

    This is the evidence undo needs to answer "is this still only what the fix
    put here?". Directories record every entry (dirs as ``d``, symlinks as
    ``l:<target>``, files by digest) up to :data:`CREATED_MANIFEST_CAP`; past the
    cap the manifest is marked truncated and undo will not delete on it.
    """
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if path.is_dir():
        entries: dict[str, str] = {}
        truncated = False
        for dirpath, dirnames, filenames in os.walk(path):
            here = Path(dirpath)
            for name in sorted(dirnames) + sorted(filenames):
                child = here / name
                rel = str(child.relative_to(path))
                if len(entries) >= CREATED_MANIFEST_CAP:
                    truncated = True
                    break
                if child.is_symlink():
                    entries[rel] = "l:" + os.readlink(child)
                elif child.is_dir():
                    entries[rel] = "d"
                else:
                    try:
                        entries[rel] = _sha256_file(child)
                    except OSError as exc:
                        entries[rel] = f"unreadable:{type(exc).__name__}"
            if truncated:
                break
        return {"kind": "dir", "entries": entries, "truncated": truncated}
    if path.exists():
        try:
            return {"kind": "file", "sha256": _sha256_file(path)}
        except OSError as exc:
            return {"kind": "file", "sha256": None, "error": f"{type(exc).__name__}: {exc}"}
    return {"kind": "absent"}


def created_still_matches(path: Path, snapshot: Mapping[str, Any] | None) -> tuple[bool, str]:
    """Is ``path`` still exactly what the fix created? ``(ok, why_not)``."""
    if not isinstance(snapshot, Mapping):
        return False, "the run artifact records no post-fix manifest for this path"
    kind = str(snapshot.get("kind") or "")
    if kind == "absent":
        if os.path.lexists(path):
            return False, "the fix created nothing here, but something exists now"
        return True, ""
    if snapshot.get("truncated"):
        return False, (
            f"the fix created more than {CREATED_MANIFEST_CAP} entries here, so the "
            "recorded manifest is incomplete; remove it by hand if you are sure"
        )
    if not os.path.lexists(path):
        return True, ""
    if kind == "symlink":
        if not path.is_symlink():
            return False, "the fix created a symlink here; it is no longer a symlink"
        if os.readlink(path) != snapshot.get("target"):
            return False, "the symlink now points somewhere else"
        return True, ""
    if path.is_symlink():
        return False, f"the fix created a {kind} here; it is a symlink now"
    if kind == "file":
        if not path.is_file():
            return False, "the fix created a file here; it is not a file now"
        try:
            if _sha256_file(path) != snapshot.get("sha256"):
                return False, "the file has been modified since the fix created it"
        except OSError as exc:
            return False, f"cannot re-read the file to compare: {type(exc).__name__}: {exc}"
        return True, ""
    if kind == "dir":
        if not path.is_dir():
            return False, "the fix created a directory here; it is not a directory now"
        recorded = dict(snapshot.get("entries") or {})
        current = snapshot_created(path)
        if current.get("truncated"):
            return False, (
                "this directory now holds more entries than the manifest cap; it has "
                "been populated since the fix created it"
            )
        live = dict(current.get("entries") or {})
        added = sorted(set(live) - set(recorded))
        changed = sorted(key for key in set(live) & set(recorded) if live[key] != recorded[key])
        if added or changed:
            detail = ", ".join((added + changed)[:5])
            more = "" if len(added + changed) <= 5 else f" (+{len(added + changed) - 5} more)"
            return False, (
                "content the fix did not create is inside this directory now: "
                f"{detail}{more}"
            )
        return True, ""
    return False, f"unrecognised manifest kind {kind!r}"


def _backup_pair(root_dir: Path, backup_root: Path, raw: str) -> tuple[Path, Path]:
    """(source, backup destination) for one declared path.

    An ABSOLUTE declared path (``~/.claude.json``) is mirrored under an
    ``_abs/`` subtree of the run's backup directory so it cannot escape the
    backup root or collide with a repo-relative capture of the same basename.
    """
    expanded = Path(str(raw)).expanduser()
    if expanded.is_absolute():
        source = expanded
        target = backup_root / "_abs" / expanded.relative_to(expanded.anchor)
    else:
        source = root_dir / expanded
        target = backup_root / expanded
    return source, target


def capture_backups(
    root_dir: Path,
    rel_paths: Iterable[str],
    backup_root: Path,
    *,
    scopes: Sequence[Path] | None = None,
) -> list[BackupEntry]:
    """Snapshot each declared path before anything mutates.

    A path that does NOT exist is still recorded (``existed: false``) so undo
    knows to DELETE whatever the fix created there — restoring "absent" is as
    much a restore as restoring bytes. Each entry also records the resolved
    absolute path and its write scope, which is what makes the undo decidable
    without re-expanding an untrusted string later.
    """
    declared = []
    seen: set[str] = set()
    for rel in rel_paths:
        rel = str(rel).strip()
        if not rel or rel in seen:
            continue
        seen.add(rel)
        declared.append((rel, resolve_no_follow(_declared_abs(root_dir, rel))))

    roots = list(scopes) if scopes is not None else write_scope_roots(
        root_dir, [resolved for _, resolved in declared]
    )

    entries: list[BackupEntry] = []
    for rel, resolved in declared:
        scope = _scope_for(roots, resolved)
        if scope is None:
            raise DoctorFixError(
                f"declared backup path {resolved} is outside every write scope; "
                "refusing to run a fix whose undo could not be contained"
            )
        source, target = _backup_pair(root_dir, backup_root, rel)
        if source.is_dir() and not source.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
            entries.append(BackupEntry(rel, str(target), True, "dir", str(resolved), str(scope)))
        elif os.path.lexists(source):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)
            entries.append(BackupEntry(rel, str(target), True, "file", str(resolved), str(scope)))
        else:
            entries.append(BackupEntry(rel, None, False, "absent", str(resolved), str(scope)))
    return entries


def record_created(entries: Sequence[BackupEntry]) -> None:
    """After the fixers ran, record what each created path now holds.

    Only ``existed=false`` entries get a manifest: those are the only ones undo
    is ever allowed to DELETE, and the manifest is the proof it may.
    """
    for entry in entries:
        if entry.existed:
            continue
        entry.created = snapshot_created(Path(entry.resolved))


# --------------------------------------------------------------------------- #
# Artifact integrity
# --------------------------------------------------------------------------- #


def integrity_key(state_root: Path, *, create: bool) -> bytes:
    """The HMAC key for this state root, created 0600 on first use.

    ``create=False`` (the undo side) REFUSES when the key is absent: no key
    means no run this engine wrote, which means the artifact came from
    somewhere else.
    """
    key_path = state_root / RUNS_DIRNAME / INTEGRITY_KEY_NAME
    try:
        raw = key_path.read_bytes()
    except FileNotFoundError:
        raw = b""
    except OSError as exc:
        raise DoctorFixError(f"cannot read {key_path}: {exc}") from exc
    if raw:
        if len(raw) < 32:
            raise DoctorFixError(f"{key_path} is truncated; refusing to trust run artifacts")
        return raw
    if not create:
        raise DoctorFixError(
            f"no signing key at {key_path}: this state root has never run a doctor fix, "
            "so the artifact cannot be one of ours"
        )
    key_path.parent.mkdir(parents=True, exist_ok=True)
    fresh = secrets.token_bytes(32)
    try:
        handle = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # a concurrent fix won the race; its key is the key
        return integrity_key(state_root, create=False)
    with os.fdopen(handle, "wb") as fh:
        fh.write(fresh)
    return fresh


def _signature(key: bytes, artifact: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {field_name: artifact.get(field_name) for field_name in SIGNED_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_artifact(artifact: dict[str, Any], key: bytes) -> dict[str, Any]:
    artifact["integrity"] = {
        "algorithm": INTEGRITY_ALGORITHM,
        "signed_fields": list(SIGNED_FIELDS),
        "signature": _signature(key, artifact),
    }
    return artifact


def verify_artifact(artifact: Mapping[str, Any], key: bytes) -> None:
    block = artifact.get("integrity")
    if not isinstance(block, Mapping):
        raise DoctorFixError("run artifact carries no integrity block; refusing to act on it")
    if block.get("algorithm") != INTEGRITY_ALGORITHM:
        raise DoctorFixError(f"unsupported artifact signature algorithm {block.get('algorithm')!r}")
    recorded = str(block.get("signature") or "")
    if not hmac.compare_digest(recorded, _signature(key, artifact)):
        raise DoctorFixError(
            "run artifact signature does not match its contents: it was hand-edited, "
            "planted, or written by a different state root. Refusing to undo."
        )


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


@dataclass
class FixRun:
    """The outcome of a ``--fix`` invocation: an artifact plus an exit code."""

    artifact: dict[str, Any]
    artifact_path: Path | None
    exit_code: int
    undo_command: str
    lines: list[str] = field(default_factory=list)


def _tail(text: str | None) -> str:
    text = text or ""
    return text[-OUTPUT_TAIL_CHARS:]


def _checked_payload(findings: Sequence[Finding]) -> list[dict[str, Any]]:
    return [
        {
            "code": f.code,
            "status": normalize_status(f.status),
            "message": f.message,
            "fixable": bool(f.fixable),
            "fix_reason": f.fix_reason
            or (REASON_HEALTHY if normalize_status(f.status) == STATUS_PASS else ""),
        }
        for f in findings
    ]


def run_fix(
    *,
    tool: str,
    root_dir: Path,
    findings: Sequence[Finding],
    registry: Mapping[str, FixSpec],
    confirmed: bool,
    boundary_id: str,
    undo_command_template: str,
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> FixRun:
    """Preview or apply the registered fixes for ``findings``.

    Without ``confirmed`` this writes a ``preview`` artifact and returns
    :data:`~doctor_contract.EXIT_NEEDS_INPUT`; the caller MUST NOT treat that as
    a failure. With ``confirmed`` it takes the lease, captures backups, runs each
    fixer in registry-declaration order, and records every outcome.
    """
    findings = annotate_fixable(list(findings), registry)
    planned = [registry[f.code] for f in findings if f.fixable and f.code in registry]
    skipped = [
        {"code": f.code, "status": normalize_status(f.status), "reason": f.fix_reason}
        for f in findings
        if not f.fixable and normalize_status(f.status) != STATUS_PASS
    ]

    run_id = uuid.uuid4().hex[:12]
    stamp = _utc_stamp()
    root_dir = Path(root_dir)
    state_root = resolve_state_root(root_dir)
    directory = runs_dir(root_dir, tool)
    artifact_path = directory / f"{stamp}-{run_id}.json"
    backup_root = directory / f"{stamp}-{run_id}.backup"
    undo_command = undo_command_template.format(artifact=str(artifact_path))

    # Computed BEFORE anything is written: a spec that declares a path outside
    # every scope refuses the whole run here, while nothing has changed.
    declared_paths = [path for spec in planned for path in spec.backup_paths]
    scopes = write_scope_roots(
        root_dir,
        [resolve_no_follow(_declared_abs(root_dir, raw)) for raw in declared_paths],
    )
    state_root.mkdir(parents=True, exist_ok=True)
    signing_key = integrity_key(state_root, create=True)

    artifact: dict[str, Any] = {
        "schema_version": RUN_ARTIFACT_SCHEMA_VERSION,
        "run_id": run_id,
        "tool": tool,
        "mode": MODE_APPLY if confirmed else MODE_PREVIEW,
        "started_at": _now_iso(),
        "finished_at": None,
        "argv": list(argv or []),
        "confirmed": bool(confirmed),
        "repo_root": str(Path(root_dir).resolve()),
        "state_root": str(state_root),
        "boundary_id": boundary_id,
        "artifact_path": str(artifact_path),
        "backup_root": str(backup_root),
        # Recorded, signed, and enforced at undo time: undo may only write
        # strictly inside one of these roots.
        "write_scope": [str(scope) for scope in scopes],
        "checked": _checked_payload(findings),
        "planned": [spec.to_payload() for spec in planned],
        "skipped": skipped,
        "backups": [],
        "applied": [],
        "undo": {
            "supported": False,
            "command": undo_command,
            "note": "nothing was changed, so there is nothing to undo",
        },
        "undo_command": undo_command,
        "summary": {
            "checked": len(findings),
            "planned": len(planned),
            "skipped": len(skipped),
            "applied": 0,
            "failed": 0,
        },
    }

    lines: list[str] = []
    if not planned:
        artifact["finished_at"] = _now_iso()
        artifact["mode"] = MODE_PREVIEW
        artifact["outcome"] = "nothing-to-fix"
        _write_artifact(artifact_path, sign_artifact(artifact, signing_key))
        lines.append(f"{tool} --fix: no auto-fixable findings.")
        for item in skipped:
            lines.append(f"  skip {item['code']} ({item['status']}): {item['reason']}")
        lines.append(f"  run artifact: {artifact_path}")
        # Nothing to fix is not "needs input" — it is simply the current verdict.
        exit_code = EXIT_DRIFT if any(
            normalize_status(f.status) == STATUS_FAIL for f in findings
        ) else EXIT_OK
        return FixRun(artifact, artifact_path, exit_code, undo_command, lines)

    if not confirmed:
        artifact["finished_at"] = _now_iso()
        artifact["outcome"] = "confirmation-required"
        _write_artifact(artifact_path, sign_artifact(artifact, signing_key))
        lines.append(f"{tool} --fix: PLAN ONLY — nothing has been changed.")
        for spec in planned:
            lines.append(f"  would fix {spec.code}: {spec.command_text}")
            lines.append(f"      {spec.description}")
            if spec.backup_paths:
                lines.append(f"      backs up: {', '.join(spec.backup_paths)}")
        for item in skipped:
            lines.append(f"  skip {item['code']} ({item['status']}): {item['reason']}")
        lines.append(f"  run artifact: {artifact_path}")
        lines.append(f"  to apply: re-run with --yes (exit {EXIT_NEEDS_INPUT} = confirmation required)")
        return FixRun(artifact, artifact_path, EXIT_NEEDS_INPUT, undo_command, lines)

    backup_paths: list[str] = []
    for spec in planned:
        backup_paths.extend(spec.backup_paths)

    run_env = dict(os.environ if env is None else env)
    applied: list[dict[str, Any]] = []
    with mutation_gate(
        root_dir,
        boundary_id,
        annotations={"tool": tool, "run_id": run_id, "codes": ",".join(s.code for s in planned)},
    ):
        backups = capture_backups(root_dir, backup_paths, backup_root, scopes=scopes)
        artifact["backups"] = [entry.to_payload() for entry in backups]
        artifact["undo"] = {
            "supported": True,
            "command": undo_command,
            "confirm_command": f"{undo_command} --yes",
            "restores": [entry.path for entry in backups],
            "note": (
                "replays every captured path in reverse. Paths recorded as existed=false "
                "are DELETED, but only while they still match the post-fix manifest — if "
                "anything else has been written there since, undo refuses that path and "
                "says so. `--undo` alone previews; `--undo --yes` acts."
            ),
        }
        # The undo path is written to disk BEFORE the first mutation, so an
        # interrupted run still leaves a usable recovery instruction on disk.
        _write_artifact(artifact_path, sign_artifact(artifact, signing_key))

        for spec in planned:
            started = time.time()
            try:
                proc = subprocess.run(
                    list(spec.command),
                    cwd=str(root_dir),
                    capture_output=True,
                    text=True,
                    timeout=spec.timeout_s,
                    env=run_env,
                )
                record = {
                    "code": spec.code,
                    "command": list(spec.command),
                    "exit_code": proc.returncode,
                    "ok": proc.returncode == 0,
                    "stdout_tail": _tail(proc.stdout),
                    "stderr_tail": _tail(proc.stderr),
                    "duration_s": round(time.time() - started, 3),
                }
            except subprocess.TimeoutExpired:
                record = {
                    "code": spec.code,
                    "command": list(spec.command),
                    "exit_code": None,
                    "ok": False,
                    "error": f"fixer exceeded its {spec.timeout_s:g}s cap",
                    "duration_s": round(time.time() - started, 3),
                }
            except OSError as exc:
                record = {
                    "code": spec.code,
                    "command": list(spec.command),
                    "exit_code": None,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration_s": round(time.time() - started, 3),
                }
            applied.append(record)
            artifact["applied"] = applied
            _write_artifact(artifact_path, sign_artifact(artifact, signing_key))

        # Post-fix evidence for the paths the fix CREATED. This is what makes a
        # later delete decidable: undo removes a created path only while it is
        # still exactly this, and refuses once anything else lives there.
        record_created(backups)
        artifact["backups"] = [entry.to_payload() for entry in backups]
        _write_artifact(artifact_path, sign_artifact(artifact, signing_key))

    failed = [record for record in applied if not record.get("ok")]
    artifact["finished_at"] = _now_iso()
    artifact["outcome"] = "applied" if not failed else "partially-applied"
    artifact["summary"].update({"applied": len(applied) - len(failed), "failed": len(failed)})
    _write_artifact(artifact_path, sign_artifact(artifact, signing_key))

    lines.append(f"{tool} --fix --yes: {len(applied) - len(failed)} applied, {len(failed)} failed.")
    for record in applied:
        marker = "ok  " if record.get("ok") else "FAIL"
        lines.append(f"  {marker} {record['code']}: {' '.join(record['command'])}")
        if not record.get("ok"):
            lines.append(f"       {record.get('error') or _tail(record.get('stderr_tail'))[-300:]}")
    for item in skipped:
        lines.append(f"  skip {item['code']} ({item['status']}): {item['reason']}")
    lines.append(f"  run artifact: {artifact_path}")
    lines.append(f"  undo (preview): {undo_command}")
    lines.append(f"  undo (apply):   {undo_command} --yes")
    lines.append("  re-run the doctor to confirm the findings cleared.")

    return FixRun(
        artifact,
        artifact_path,
        EXIT_OK if not failed else EXIT_ERROR_ON_FIXER_FAILURE,
        undo_command,
        lines,
    )


def load_undo_artifact(artifact_path: Path, root_dir: Path) -> dict[str, Any]:
    """Read + AUTHENTICATE a run artifact. Never a trusting read.

    The artifact is a JSON file in a directory the workspace can write, so every
    property undo relies on is checked here, before a plan even exists:

    1. it is a v2 doctor artifact (a v1 one is refused, not migrated);
    2. it LIVES in this state root's run directory — a path handed on the
       command line that resolves anywhere else is refused outright, which is
       what stops ``--undo /tmp/planted.json``;
    3. it names THIS repo root, so an artifact from another checkout cannot
       drive writes here;
    4. its signature matches, so its path list is the one the fix recorded.
    """
    root_dir = Path(root_dir).resolve()
    given = Path(artifact_path)
    resolved = Path(os.path.realpath(str(given if given.is_absolute() else root_dir / given)))
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise DoctorFixError(f"cannot read run artifact {given}: {exc}") from exc

    state_root = resolve_state_root(root_dir)
    runs_root = Path(os.path.realpath(str(state_root / RUNS_DIRNAME)))
    if not _strictly_inside(runs_root, resolved):
        raise DoctorFixError(
            f"{resolved} is not inside this state root's run directory ({runs_root}); "
            "undo only replays artifacts the doctor itself wrote"
        )

    try:
        artifact = json.loads(text)
    except ValueError as exc:
        raise DoctorFixError(f"cannot parse run artifact {given}: {exc}") from exc
    if not isinstance(artifact, dict):
        raise DoctorFixError(f"{given} is not a doctor run artifact (expected a JSON object)")
    if artifact.get("schema_version") != RUN_ARTIFACT_SCHEMA_VERSION:
        raise DoctorFixError(
            f"{given} is not a {RUN_ARTIFACT_SCHEMA_VERSION} doctor run artifact "
            f"(found {artifact.get('schema_version')!r})"
        )
    recorded_root = str(artifact.get("repo_root") or "")
    if recorded_root and Path(recorded_root) != root_dir:
        raise DoctorFixError(
            f"{given} was written for repo root {recorded_root}, not {root_dir}; refusing to undo"
        )
    verify_artifact(artifact, integrity_key(state_root, create=False))
    artifact["_resolved_artifact_path"] = str(resolved)
    return artifact


def plan_undo(artifact: Mapping[str, Any], root_dir: Path) -> list[dict[str, Any]]:
    """Decide, without touching anything, what undo would do to each path.

    Every action is derived from what the FIX recorded, never from re-expanding
    a string now. A path fails one of four ways, and all four fail closed:

    * it is not inside a recorded write-scope root (``..``, an absolute path
      somebody appended, a scope the run never declared);
    * it no longer resolves where the fix resolved it (an ancestor symlink was
      swapped in after the fact);
    * it would be a DELETE without proof the fix created it;
    * it would be a delete of something that no longer matches the post-fix
      manifest — the ``sync`` populated ``repos/`` case. That one is the whole
      reason this function exists: refusing loudly and leaving the directory is
      strictly better than removing work nobody asked us to remove.
    """
    root_dir = Path(root_dir).resolve()
    scopes = [Path(str(scope)) for scope in (artifact.get("write_scope") or [])]
    if not scopes:
        raise DoctorFixError("run artifact records no write scope; refusing to undo")

    plan: list[dict[str, Any]] = []
    for entry in reversed(list(artifact.get("backups") or [])):
        declared = str(entry.get("path") or "")
        recorded = str(entry.get("resolved") or "")
        row: dict[str, Any] = {"path": declared, "resolved": recorded}
        if not recorded or not Path(recorded).is_absolute():
            row.update(action="refused", ok=False, reason="entry records no resolved path")
            plan.append(row)
            continue

        target = Path(recorded)
        scope = _scope_for(scopes, target)
        if scope is None:
            row.update(
                action="refused",
                ok=False,
                reason=f"{target} is outside every recorded write scope "
                f"({', '.join(str(s) for s in scopes)})",
            )
            plan.append(row)
            continue
        row["scope"] = str(scope)

        live = resolve_no_follow(target)
        if live != target:
            row.update(
                action="refused",
                ok=False,
                reason=f"{target} now resolves to {live}; a symlink changed under it since the fix",
            )
            plan.append(row)
            continue

        if not bool(entry.get("existed")):
            ok, why = created_still_matches(target, entry.get("created"))
            if not ok:
                row.update(action="refused", ok=False, reason=why)
            elif not os.path.lexists(target):
                row.update(action="already-absent", ok=None)
            elif target.is_symlink():
                row.update(action="remove-symlink", ok=None)
            elif target.is_dir():
                row.update(action="remove-dir", ok=None)
            else:
                row.update(action="remove-file", ok=None)
            plan.append(row)
            continue

        backup = str(entry.get("backup") or "")
        if not backup or not os.path.lexists(backup):
            row.update(
                action="refused",
                ok=False,
                reason=f"backup {backup or '(none recorded)'} is gone; cannot restore",
            )
            plan.append(row)
            continue
        row["backup"] = backup
        row.update(action="restore-dir" if Path(backup).is_dir() else "restore-file", ok=None)
        plan.append(row)
    return plan


def apply_undo(plan: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Execute a validated :func:`plan_undo`. Refusals pass through untouched."""
    results: list[dict[str, Any]] = []
    for row in plan:
        result = dict(row)
        action = str(row.get("action") or "")
        target = Path(str(row.get("resolved")))
        try:
            if action in ("refused", "already-absent"):
                result["ok"] = bool(row.get("ok")) if action == "refused" else True
            elif action == "remove-dir":
                shutil.rmtree(target)
                result["ok"] = True
            elif action in ("remove-file", "remove-symlink"):
                target.unlink()
                result["ok"] = True
            elif action == "restore-dir":
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                elif os.path.lexists(target):
                    target.unlink()
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(Path(str(row["backup"])), target, symlinks=True, dirs_exist_ok=True)
                result["ok"] = True
            elif action == "restore-file":
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                elif os.path.lexists(target):
                    target.unlink()
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(str(row["backup"])), target, follow_symlinks=False)
                result["ok"] = True
            else:  # pragma: no cover - plan_undo emits nothing else
                result.update(ok=False, reason=f"unrecognised undo action {action!r}")
        except OSError as exc:
            result.update(ok=False, action="error", reason=f"{type(exc).__name__}: {exc}")
        results.append(result)
    return results


def undo_run(
    artifact_path: Path, *, root_dir: Path | None = None, confirmed: bool = False
) -> FixRun:
    """Preview (default) or apply the undo of a previous ``--fix --yes`` run.

    Without ``confirmed`` this is a PLAN: it reads, authenticates, validates,
    prints exactly what it would restore and delete, and exits
    :data:`~doctor_contract.EXIT_NEEDS_INPUT`. Deleting files is at least as
    consequential as creating them, so undo carries the same confirmation
    contract as ``--fix``: ``--undo`` looks, ``--undo --yes`` acts.
    """
    if root_dir is None:
        raise DoctorFixError("undo requires an explicit repo root")
    repo_root = Path(root_dir).resolve()
    artifact = load_undo_artifact(Path(artifact_path), repo_root)
    resolved_artifact = Path(str(artifact.pop("_resolved_artifact_path")))
    if not (artifact.get("backups") or []):
        raise DoctorFixError(
            f"{resolved_artifact} captured no backups (mode={artifact.get('mode')}); "
            "nothing to undo"
        )

    plan = plan_undo(artifact, repo_root)
    boundary_id = str(artifact.get("boundary_id") or "")
    undo_block = artifact.get("undo")
    undo_command = str(
        (undo_block or {}).get("command") if isinstance(undo_block, Mapping) else ""
    ) or str(artifact.get("undo_command") or "")

    if confirmed:
        with mutation_gate(
            repo_root, boundary_id, annotations={"undo_of": str(artifact.get("run_id") or "")}
        ):
            results = apply_undo(plan)
        mode, outcome_verb = MODE_UNDO, "undone"
    else:
        results = [dict(row) for row in plan]
        mode, outcome_verb = MODE_PREVIEW, "planned"

    refused = [row for row in results if row.get("action") == "refused" or row.get("ok") is False]
    acted = [row for row in results if row.get("action") != "refused"]
    record: dict[str, Any] = {
        "schema_version": RUN_ARTIFACT_SCHEMA_VERSION,
        "run_id": uuid.uuid4().hex[:12],
        "tool": artifact.get("tool"),
        "mode": mode,
        "operation": MODE_UNDO,
        "confirmed": bool(confirmed),
        "undo_of": artifact.get("run_id"),
        "undo_of_artifact": str(resolved_artifact),
        "started_at": _now_iso(),
        "finished_at": _now_iso(),
        "repo_root": str(repo_root),
        "state_root": str(resolve_state_root(repo_root)),
        "boundary_id": boundary_id,
        "write_scope": list(artifact.get("write_scope") or []),
        "restored": results,
        "outcome": (
            "confirmation-required"
            if not confirmed
            else ("undone" if not refused else "partially-undone")
        ),
        "summary": {
            outcome_verb: len(acted),
            "refused": len(refused),
        },
    }
    out_path = resolved_artifact.with_suffix(".undo.json" if confirmed else ".undo-preview.json")
    _write_artifact(out_path, record)

    verb = "would" if not confirmed else "did"
    lines = [
        f"undo of {resolved_artifact}: {len(acted)} {verb} apply, {len(refused)} refused."
        if not confirmed
        else f"undo of {resolved_artifact}: {len(acted)} applied, {len(refused)} refused."
    ]
    for row in results:
        if row.get("action") == "refused" or row.get("ok") is False:
            lines.append(f"  REFUSED {row['path']}: {row.get('reason', '')}")
        else:
            lines.append(f"  {'would ' if not confirmed else ''}{row['action']} {row['path']}")
    lines.append(f"  undo artifact: {out_path}")
    if not confirmed:
        lines.append(
            f"  PLAN ONLY — nothing has been changed. To apply: {undo_command} --yes "
            f"(exit {EXIT_NEEDS_INPUT} = confirmation required)"
        )
    exit_code = (
        EXIT_NEEDS_INPUT
        if not confirmed
        else (EXIT_OK if not refused else EXIT_ERROR_ON_FIXER_FAILURE)
    )
    return FixRun(record, out_path, exit_code, undo_command, lines)


def _write_artifact(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - exotic filesystem
        pass


__all__ = [
    "CREATED_MANIFEST_CAP",
    "INTEGRITY_ALGORITHM",
    "INTEGRITY_KEY_NAME",
    "RUN_ARTIFACT_SCHEMA_VERSION",
    "RUNS_DIRNAME",
    "SIGNED_FIELDS",
    "MODE_APPLY",
    "MODE_PREVIEW",
    "MODE_UNDO",
    "BackupEntry",
    "DoctorFixError",
    "FixRun",
    "FixSpec",
    "annotate_fixable",
    "apply_undo",
    "build_registry",
    "capture_backups",
    "created_still_matches",
    "integrity_key",
    "load_undo_artifact",
    "mutation_gate",
    "plan_undo",
    "record_created",
    "resolve_no_follow",
    "resolve_state_root",
    "run_fix",
    "runs_dir",
    "sign_artifact",
    "snapshot_created",
    "undo_run",
    "verify_artifact",
    "write_scope_roots",
]
