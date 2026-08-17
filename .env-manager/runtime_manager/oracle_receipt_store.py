"""Atomic read/write protocol for the Oracle browser receipt.

The launcher used to clear a stale receipt with ``os.unlink(browser.json)``,
fsync the directory, and only publish the new receipt once the browser was up.
Between those two moments — the whole of a browser launch, seconds, not
microseconds — ``browser.json`` simply did not exist. Any concurrent reader
(``oracle-subagent-auth.mjs status --json``, a heal check, a second pane) opened
nothing, failed its object check, and reported ``browser_receipt_invalid``.

The visible damage was a *lie about a healthy session*: `status` flapped between
``ready`` with no reasons and ``blocked`` with ``browser_receipt_invalid``
seconds apart while the on-disk receipt was complete and valid, so agents healed,
relaunched, or abandoned runs that were fine. It got worse with concurrency —
multi-pane swarms, or fleet RPC and a local ask at once.

Two changes close it, and this module is the reference implementation of both:

1. **Never unlink.** :func:`invalidate_receipt` publishes a complete, valid,
   explicitly *not ready* receipt through the same atomic path as a real one.
   A reader therefore never sees a missing or half-written file — it sees a
   truthful document that says "this browser is not ready", which is a
   different and honest answer. Publication is temp-sibling → ``0600`` →
   fsync file → ``os.replace`` → fsync directory, so readers observe either the
   old receipt or the new one and nothing in between.

2. **Retry once.** :func:`read_receipt` retries a vanished or unparseable
   receipt before declaring it invalid, so a reader that races a publication
   still gets the truth. This is defence in depth: with an atomic writer the
   window is gone, but a reader must not depend on every writer in the estate
   having been fixed.

The reader also opens first and validates the *opened descriptor*, rather than
lstat-ing a path and opening it afterwards. A check-then-open pair has its own
race — ``os.replace`` can swap the inode in between, and a reader that compares
the two snapshots reports a corrupt receipt when all it really saw was a
successful publish.

This module owns the I/O protocol only. Receipt *semantics* — the 28-key
contract and the ownership fields — belong to the doctor, and receipt
*freshness* belongs to :mod:`runtime_manager.oracle_receipt`.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from .oracle_broker import OracleBrokerError

ORACLE_BROWSER_RECEIPT_SCHEMA = "oracle-subagent.browser.v1"
ORACLE_BROWSER_TEST_RECEIPT_SCHEMA = "oracle-subagent.browser-test.v1"
RECEIPT_SCHEMAS = frozenset(
    {ORACLE_BROWSER_RECEIPT_SCHEMA, ORACLE_BROWSER_TEST_RECEIPT_SCHEMA}
)

RECEIPT_FILENAME = "browser.json"
TEMP_PREFIX = ".browser-"
TEMP_SUFFIX = ".tmp"

#: A receipt is a small JSON envelope; anything larger is not one.
MAX_RECEIPT_BYTES = 256 * 1024

#: One retry is enough to cross a publication: the writer holds the window for
#: the duration of a rename, not a launch. More retries would only delay an
#: honest failure.
DEFAULT_READ_RETRIES = 1
RETRY_BACKOFF_SECONDS = 0.01

STATE_READY = "ready"
STATE_TEST_READY = "test_ready"
STATE_INVALIDATED = "invalidated"

#: Reason codes carried by an invalidated receipt. Free text is deliberately not
#: accepted: a receipt is read by machines and must not carry host detail.
INVALIDATION_REASONS = frozenset(
    {
        "launcher_restart",
        "launch_failed",
        "browser_lost",
        "operator_reset",
        "superseded",
    }
)

REFUSAL_CODES = frozenset(
    {
        "browser_receipt_invalid",
        "receipt_root_invalid",
        "receipt_write_failed",
        "wrong_permissions",
    }
)


class OracleReceiptStoreError(OracleBrokerError):
    """Stable, non-sensitive receipt I/O refusal."""


def _refuse(code: str) -> Any:
    raise OracleReceiptStoreError(code)


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # pragma: no cover - POSIX-only contract
        _refuse("wrong_permissions")
    return int(getuid())


def receipt_path(runtime_root: Any) -> Path:
    """Where the browser receipt lives under a runtime root."""

    if isinstance(runtime_root, Path):
        root = runtime_root
    elif isinstance(runtime_root, str) and runtime_root:
        root = Path(runtime_root)
    else:
        _refuse("receipt_root_invalid")
    return root / RECEIPT_FILENAME


def _verified_runtime_root(runtime_root: Any, uid: int) -> Path:
    root = receipt_path(runtime_root).parent
    try:
        metadata = os.lstat(root)
    except OSError:
        _refuse("receipt_root_invalid")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        _refuse("receipt_root_invalid")
    if metadata.st_uid != uid or stat.S_IMODE(metadata.st_mode) & 0o077:
        # A world- or group-accessible runtime root means another account could
        # publish a receipt of its own choosing.
        _refuse("wrong_permissions")
    return root


def _validated_document(value: Any) -> dict[str, Any]:
    """Envelope check only. The 28-key contract belongs to the doctor."""

    if not isinstance(value, Mapping):
        _refuse("browser_receipt_invalid")
    document = dict(value)
    schema = document.get("schema")
    if type(schema) is not str or schema not in RECEIPT_SCHEMAS:
        _refuse("browser_receipt_invalid")
    state = document.get("state")
    if type(state) is not str or not state or len(state) > 64:
        _refuse("browser_receipt_invalid")
    return document


def _encode(document: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        _refuse("browser_receipt_invalid")
    if len(encoded) > MAX_RECEIPT_BYTES:
        _refuse("browser_receipt_invalid")
    return encoded


def publish_receipt(runtime_root: Any, document: Any, *, uid: int | None = None) -> Path:
    """Publish a receipt atomically. Readers never observe a partial state.

    temp sibling in the same directory → ``0600`` → write → fsync the file →
    ``os.replace`` → fsync the directory. Same-directory placement is what makes
    the rename atomic; a temp file elsewhere could land on another filesystem
    and silently degrade to a copy.
    """

    resolved_uid = _current_uid() if uid is None else uid
    root = _verified_runtime_root(runtime_root, resolved_uid)
    target = receipt_path(runtime_root)
    encoded = _encode(_validated_document(document))

    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=root, prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX
        )
    except OSError:
        _refuse("receipt_write_failed")
    try:
        try:
            os.fchmod(descriptor, 0o600)
            written = 0
            while written < len(encoded):
                written += os.write(descriptor, encoded[written:])
            os.fsync(descriptor)
        except OSError:
            _refuse("receipt_write_failed")
        os.close(descriptor)
        descriptor = -1
        try:
            os.replace(temporary, target)
        except OSError:
            _refuse("receipt_write_failed")
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise

    directory = os.open(root, os.O_RDONLY)
    try:
        try:
            os.fsync(directory)
        except OSError:
            # os.replace is already atomic; the directory fsync is durability,
            # and not every filesystem allows it on a directory handle.
            pass
    finally:
        os.close(directory)
    return target


def invalidate_receipt(
    runtime_root: Any,
    reason: str = "launcher_restart",
    *,
    schema: str = ORACLE_BROWSER_RECEIPT_SCHEMA,
    uid: int | None = None,
) -> Path:
    """Retire a receipt by publishing a not-ready one, never by unlinking.

    This is the fix for the original defect. Clearing a stale receipt is a real
    requirement — a previous ready receipt must never be evidence for the next
    launch — but *deleting* it makes every concurrent reader report a corrupt
    receipt for the whole of a launch. Publishing an explicit ``invalidated``
    receipt satisfies the requirement and tells readers the truth.
    """

    if type(reason) is not str or reason not in INVALIDATION_REASONS:
        _refuse("browser_receipt_invalid")
    if type(schema) is not str or schema not in RECEIPT_SCHEMAS:
        _refuse("browser_receipt_invalid")
    return publish_receipt(
        runtime_root,
        {"schema": schema, "state": STATE_INVALIDATED, "reason": reason},
        uid=uid,
    )


def _read_once(target: Path, uid: int) -> dict[str, Any]:
    """One attempt. Opens first, then validates the OPENED descriptor.

    An lstat-then-open pair races ``os.replace``: the inode can change between
    the two, and a reader comparing the snapshots reports a corrupt receipt when
    all it saw was a successful publish. ``O_NOFOLLOW`` covers the symlink case
    that lstat was there for.
    """

    descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            _refuse("browser_receipt_invalid")
        if metadata.st_uid != uid or stat.S_IMODE(metadata.st_mode) & 0o077:
            _refuse("wrong_permissions")
        if metadata.st_size > MAX_RECEIPT_BYTES:
            _refuse("browser_receipt_invalid")
        raw = os.read(descriptor, MAX_RECEIPT_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_RECEIPT_BYTES:
        _refuse("browser_receipt_invalid")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        _refuse("browser_receipt_invalid")
    return _validated_document(document)


def read_receipt(
    runtime_root: Any,
    *,
    retries: int = DEFAULT_READ_RETRIES,
    backoff_seconds: float = RETRY_BACKOFF_SECONDS,
    uid: int | None = None,
) -> dict[str, Any]:
    """Read the receipt, retrying a vanished or unparseable one before failing.

    A retry is offered for exactly the conditions a concurrent publication can
    produce — the file is briefly absent, or was read mid-swap. A permissions
    failure is never retried: that is a standing fact about the file, and
    retrying it would only delay an honest refusal.
    """

    resolved_uid = _current_uid() if uid is None else uid
    if type(retries) is not int or not 0 <= retries <= 8:
        _refuse("browser_receipt_invalid")
    if (
        isinstance(backoff_seconds, bool)
        or not isinstance(backoff_seconds, (int, float))
        or not 0 <= backoff_seconds <= 1
    ):
        _refuse("browser_receipt_invalid")

    target = receipt_path(runtime_root)
    attempts = retries + 1
    for attempt in range(attempts):
        try:
            return _read_once(target, resolved_uid)
        except OracleReceiptStoreError as error:
            if error.code == "wrong_permissions":
                raise
            if attempt + 1 >= attempts:
                raise
        except FileNotFoundError:
            if attempt + 1 >= attempts:
                _refuse("browser_receipt_invalid")
        except OSError as error:
            # ELOOP means the path is a symlink now; that is a standing state,
            # not a race, so it fails immediately.
            if error.errno == errno.ELOOP:
                _refuse("wrong_permissions")
            if attempt + 1 >= attempts:
                _refuse("browser_receipt_invalid")
        if backoff_seconds:
            time.sleep(backoff_seconds)
    # Unreachable: the final attempt always returns or raises.
    _refuse("browser_receipt_invalid")


def receipt_state(runtime_root: Any, **kwargs: Any) -> str:
    """The receipt's own state token, or a refusal. Convenience for callers."""

    return str(read_receipt(runtime_root, **kwargs)["state"])


__all__ = [
    "DEFAULT_READ_RETRIES",
    "INVALIDATION_REASONS",
    "MAX_RECEIPT_BYTES",
    "ORACLE_BROWSER_RECEIPT_SCHEMA",
    "ORACLE_BROWSER_TEST_RECEIPT_SCHEMA",
    "RECEIPT_FILENAME",
    "RECEIPT_SCHEMAS",
    "REFUSAL_CODES",
    "RETRY_BACKOFF_SECONDS",
    "STATE_INVALIDATED",
    "STATE_READY",
    "STATE_TEST_READY",
    "OracleReceiptStoreError",
    "invalidate_receipt",
    "publish_receipt",
    "read_receipt",
    "receipt_path",
    "receipt_state",
]
