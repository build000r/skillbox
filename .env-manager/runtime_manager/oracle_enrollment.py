"""Ephemeral secret handoff for Oracle enrollment, with verifier-only state.

Enrollment used to mint a VNC secret and then write it, in clear, into a
mode-0600 state file (`VNC_PASSWORD=<secret>`) so the operator could read it
back and paste it into noVNC. Mode 0600 bounds *who* can read it; it does
nothing about *how long* it exists. The secret outlived the session, survived
reboots, and sat on disk until teardown — for a credential whose only job is to
be typed once.

This module replaces that with the two things the bead asks for:

* **Ephemeral one-time handoff.** :class:`EnrollmentSecret` holds the secret in
  memory and discloses it exactly once. A second `disclose()` refuses. Nothing
  writes it anywhere.
* **Verifier-only durable state.** :class:`EnrollmentState` persists a PBKDF2
  verifier — salt, iterations, digest — which answers "is this the secret I
  minted?" and cannot produce the secret. That is all `status` and an
  identity-bound teardown ever needed.

Restart is therefore fail closed by construction, not by discipline: after a
restart the process holds a verifier and no secret, and there is deliberately no
API that turns one into the other. The honest outcome is re-enrollment, and
:func:`requires_reenrollment` says so.

Two guards make the write path hard to regress. The state document has an exact
key allowlist, and :func:`assert_payload_secret_free` re-scans the rendered
document for anything secret-shaped before it reaches the disk — so a future
caller that stuffs a secret into a field gets a refusal rather than a file.

**An honest limit.** Python strings are immutable and interned by the runtime;
``wipe()`` drops this object's reference and cannot scrub every copy the
interpreter made. The guarantee here is not "erased from RAM" — it is that the
secret is never written to durable state, never rendered by any method on these
objects, and never placed in a process argument. `ssh_handoff_argv` builds a
command that reads the secret from stdin and refuses to carry it in argv at all.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .oracle_broker import OracleBrokerError

ORACLE_ENROLLMENT_STATE_SCHEMA = "skillbox.oracle-enrollment.v1"
ENROLLMENT_STATE_REL_PATH = ("oracle", "enrollment.json")

#: The shell contract's own secret shape (`^[A-Za-z0-9_-]{24,128}$`), kept
#: byte-compatible so a ported client and this module agree on what is valid.
SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{24,128}$")
SECRET_ENTROPY_BYTES = 32

VERIFIER_ALGORITHM = "pbkdf2-hmac-sha256"
VERIFIER_ITERATIONS = 200_000
VERIFIER_SALT_BYTES = 16
VERIFIER_DIGEST_BYTES = 32
HEX_PATTERN = re.compile(r"^[0-9a-f]+$")

HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$")
MIN_PORT = 1024
MAX_PORT = 65535
MAX_STATE_BYTES = 16 * 1024
MAX_TIMESTAMP_MS = 4_102_444_800_000

STATE_KEYS = frozenset(
    {
        "schema",
        "host",
        "local_port",
        "web_port",
        "remote_script",
        "verifier",
        "created_at_ms",
    }
)
VERIFIER_KEYS = frozenset({"algorithm", "salt", "iterations", "digest"})

REFUSAL_CODES = frozenset(
    {
        "enrollment_state_invalid",
        "enrollment_state_permissions",
        "secret_already_disclosed",
        "secret_in_argv_forbidden",
        "secret_invalid",
        "secret_leak_blocked",
        "secret_unavailable",
        "verifier_invalid",
    }
)


class OracleEnrollmentError(OracleBrokerError):
    """Stable, non-sensitive enrollment refusal. Never carries the secret."""


def _refuse(code: str) -> Any:
    raise OracleEnrollmentError(code)


def _bounded_int(value: Any, minimum: int, maximum: int, code: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _refuse(code)
    return value


def _hex(value: Any, length: int, code: str) -> str:
    if (
        type(value) is not str
        or len(value) != length
        or HEX_PATTERN.fullmatch(value) is None
    ):
        _refuse(code)
    return value


def _validated_secret(value: Any) -> str:
    if type(value) is not str or SECRET_PATTERN.fullmatch(value) is None:
        # Note the refusal names the shape, never the value.
        _refuse("secret_invalid")
    return value


# --------------------------------------------------------------------------- #
# Verifier — the only representation that may be persisted
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SecretVerifier:
    """A one-way representation. Answers "is this it?" and nothing else."""

    algorithm: str
    salt: str
    iterations: int
    digest: str

    def __post_init__(self) -> None:
        if self.algorithm != VERIFIER_ALGORITHM:
            _refuse("verifier_invalid")
        _hex(self.salt, VERIFIER_SALT_BYTES * 2, "verifier_invalid")
        _hex(self.digest, VERIFIER_DIGEST_BYTES * 2, "verifier_invalid")
        _bounded_int(self.iterations, 100_000, 5_000_000, "verifier_invalid")

    @classmethod
    def for_secret(cls, secret: str, *, iterations: int = VERIFIER_ITERATIONS) -> SecretVerifier:
        validated = _validated_secret(secret)
        salt = secrets.token_bytes(VERIFIER_SALT_BYTES)
        digest = hashlib.pbkdf2_hmac(
            "sha256", validated.encode("utf-8"), salt, iterations, VERIFIER_DIGEST_BYTES
        )
        return cls(
            algorithm=VERIFIER_ALGORITHM,
            salt=salt.hex(),
            iterations=iterations,
            digest=digest.hex(),
        )

    def matches(self, candidate: Any) -> bool:
        """Constant-time check. A malformed candidate is False, not an error."""

        if type(candidate) is not str or SECRET_PATTERN.fullmatch(candidate) is None:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            candidate.encode("utf-8"),
            bytes.fromhex(self.salt),
            self.iterations,
            VERIFIER_DIGEST_BYTES,
        )
        return hmac.compare_digest(digest.hex(), self.digest)

    def to_payload(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "salt": self.salt,
            "iterations": self.iterations,
            "digest": self.digest,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> SecretVerifier:
        if not isinstance(value, Mapping) or set(value) != set(VERIFIER_KEYS):
            _refuse("verifier_invalid")
        return cls(
            algorithm=value["algorithm"],
            salt=value["salt"],
            iterations=value["iterations"],
            digest=value["digest"],
        )


# --------------------------------------------------------------------------- #
# The secret itself — memory only, disclosed once
# --------------------------------------------------------------------------- #


class EnrollmentSecret:
    """A minted secret that may be read exactly once and is never stored.

    ``disclose()`` is the single accessor and it is one-shot: the operator (or
    the ssh handoff) takes it, and every later attempt refuses. ``__repr__`` and
    ``__str__`` are redacted so an accidental print, traceback, or f-string
    cannot emit it.
    """

    __slots__ = ("_disclosed", "_secret", "_verifier")

    def __init__(self, secret: str) -> None:
        self._secret: str | None = _validated_secret(secret)
        self._disclosed = False
        self._verifier = SecretVerifier.for_secret(self._secret)

    @property
    def disclosed(self) -> bool:
        return self._disclosed

    @property
    def available(self) -> bool:
        return self._secret is not None and not self._disclosed

    @property
    def verifier(self) -> SecretVerifier:
        """Safe to persist, safe to print."""

        return self._verifier

    def disclose(self) -> str:
        """Hand the secret over exactly once, then forget this reference."""

        if self._disclosed:
            # Checked before the None test so a second take reports "already
            # disclosed" rather than the vaguer "unavailable" — an operator
            # needs to tell "someone already has it" from "there never was one".
            _refuse("secret_already_disclosed")
        if self._secret is None:
            _refuse("secret_unavailable")
        secret = self._secret
        self._disclosed = True
        self._secret = None
        return secret

    def wipe(self) -> None:
        """Drop the reference. Idempotent; see the module note on limits."""

        self._secret = None
        self._disclosed = True

    def matches(self, candidate: Any) -> bool:
        return self._verifier.matches(candidate)

    def __repr__(self) -> str:
        return f"<EnrollmentSecret disclosed={self._disclosed} secret=REDACTED>"

    __str__ = __repr__

    def __format__(self, _spec: str) -> str:
        # Without this an f-string with a format spec would bypass __str__.
        return self.__repr__()

    def __reduce__(self) -> Any:
        # Pickling would put the secret in a byte stream someone will persist.
        _refuse("secret_leak_blocked")

    def __enter__(self) -> EnrollmentSecret:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.wipe()


def mint_enrollment_secret(*, entropy_bytes: int = SECRET_ENTROPY_BYTES) -> EnrollmentSecret:
    """Mint a fresh secret. Matches the shell client's token_urlsafe(32) shape."""

    _bounded_int(entropy_bytes, 18, 96, "secret_invalid")
    return EnrollmentSecret(secrets.token_urlsafe(entropy_bytes))


# --------------------------------------------------------------------------- #
# Durable state — verifier only, written atomically and privately
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EnrollmentState:
    """What survives a restart. Deliberately cannot reconstruct the secret."""

    host: str
    local_port: int
    web_port: int
    remote_script: str
    verifier: SecretVerifier
    created_at_ms: int

    def __post_init__(self) -> None:
        if type(self.host) is not str or HOST_PATTERN.fullmatch(self.host) is None:
            _refuse("enrollment_state_invalid")
        _bounded_int(self.local_port, MIN_PORT, MAX_PORT, "enrollment_state_invalid")
        _bounded_int(self.web_port, MIN_PORT, MAX_PORT, "enrollment_state_invalid")
        if (
            type(self.remote_script) is not str
            or not self.remote_script
            or len(self.remote_script) > 512
            or "\x00" in self.remote_script
        ):
            _refuse("enrollment_state_invalid")
        if not isinstance(self.verifier, SecretVerifier):
            _refuse("verifier_invalid")
        _bounded_int(self.created_at_ms, 0, MAX_TIMESTAMP_MS, "enrollment_state_invalid")

    def to_payload(self) -> dict[str, Any]:
        """The document written to disk. There is no secret field to omit."""

        return {
            "schema": ORACLE_ENROLLMENT_STATE_SCHEMA,
            "host": self.host,
            "local_port": self.local_port,
            "web_port": self.web_port,
            "remote_script": self.remote_script,
            "verifier": self.verifier.to_payload(),
            "created_at_ms": self.created_at_ms,
        }

    def __repr__(self) -> str:
        return (
            f"<EnrollmentState host={self.host} local_port={self.local_port} "
            "verifier=<pbkdf2>>"
        )

    __str__ = __repr__

    @classmethod
    def from_mapping(cls, value: Any) -> EnrollmentState:
        if not isinstance(value, Mapping) or set(value) != set(STATE_KEYS):
            _refuse("enrollment_state_invalid")
        if value["schema"] != ORACLE_ENROLLMENT_STATE_SCHEMA:
            _refuse("enrollment_state_invalid")
        return cls(
            host=value["host"],
            local_port=value["local_port"],
            web_port=value["web_port"],
            remote_script=value["remote_script"],
            verifier=SecretVerifier.from_mapping(value["verifier"]),
            created_at_ms=value["created_at_ms"],
        )

    @classmethod
    def for_session(
        cls,
        *,
        host: str,
        local_port: int,
        web_port: int,
        remote_script: str,
        secret: EnrollmentSecret,
        now_ms: int | None = None,
    ) -> EnrollmentState:
        """Build state from a minted secret WITHOUT ever holding the secret."""

        if not isinstance(secret, EnrollmentSecret):
            _refuse("secret_invalid")
        return cls(
            host=host,
            local_port=local_port,
            web_port=web_port,
            remote_script=remote_script,
            verifier=secret.verifier,
            created_at_ms=int(time.time() * 1000) if now_ms is None else now_ms,
        )


def assert_payload_secret_free(payload: Any) -> None:
    """Refuse any document carrying something shaped like a live secret.

    Belt and braces over the key allowlist: a future caller that adds a field
    holding the secret gets ``secret_leak_blocked`` instead of a file on disk.
    The verifier's hex fields are exempt because a digest is not a secret — and
    they are separately pinned to hex of an exact length.
    """

    def scan(value: Any, inside_verifier: bool) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                scan(item, inside_verifier or key == "verifier")
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                scan(item, inside_verifier)
            return
        if isinstance(value, str) and not inside_verifier:
            if SECRET_PATTERN.fullmatch(value) is not None:
                _refuse("secret_leak_blocked")

    scan(payload, False)


def enrollment_state_path(state_root: Any) -> Path:
    if isinstance(state_root, Path):
        root = state_root
    elif isinstance(state_root, str) and state_root:
        root = Path(state_root)
    else:
        _refuse("enrollment_state_invalid")
    return root.joinpath(*ENROLLMENT_STATE_REL_PATH)


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # pragma: no cover - POSIX-only contract
        _refuse("enrollment_state_permissions")
    return int(getuid())


def write_enrollment_state(
    state_root: Any,
    state: Any,
    *,
    uid: int | None = None,
) -> Path:
    """Persist the verifier-only state atomically and privately.

    ``mkstemp`` creates the temp file 0600 with an unpredictable name, so unlike
    a `"$file.new.$$"` + `chmod` sequence there is no window in which the file
    exists world-readable and no name an attacker can pre-create.
    """

    if not isinstance(state, EnrollmentState):
        _refuse("enrollment_state_invalid")
    resolved_uid = _current_uid() if uid is None else uid
    payload = state.to_payload()
    assert_payload_secret_free(payload)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        _refuse("enrollment_state_invalid")

    target = enrollment_state_path(state_root)
    directory = target.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        metadata = os.lstat(directory)
    except OSError:
        _refuse("enrollment_state_permissions")
    if metadata.st_uid != resolved_uid or metadata.st_mode & (
        stat.S_IRWXG | stat.S_IRWXO
    ):
        _refuse("enrollment_state_permissions")

    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=directory, prefix=".enrollment-", suffix=".tmp"
        )
    except OSError:
        _refuse("enrollment_state_permissions")
    try:
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, target)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target


def read_enrollment_state(state_root: Any, *, uid: int | None = None) -> EnrollmentState:
    """Read the verifier-only state. Absence raises FileNotFoundError."""

    resolved_uid = _current_uid() if uid is None else uid
    target = enrollment_state_path(state_root)
    descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _refuse("enrollment_state_permissions")
        if metadata.st_uid != resolved_uid or stat.S_IMODE(metadata.st_mode) & 0o077:
            _refuse("enrollment_state_permissions")
        if metadata.st_size > MAX_STATE_BYTES:
            _refuse("enrollment_state_invalid")
        raw = os.read(descriptor, MAX_STATE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_STATE_BYTES:
        _refuse("enrollment_state_invalid")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        _refuse("enrollment_state_invalid")
    return EnrollmentState.from_mapping(document)


def clear_enrollment_state(state_root: Any) -> bool:
    """Teardown. Idempotent, and leaves no temp files behind."""

    target = enrollment_state_path(state_root)
    removed = False
    try:
        os.unlink(target)
        removed = True
    except FileNotFoundError:
        pass
    except OSError:
        _refuse("enrollment_state_permissions")
    directory = target.parent
    if directory.is_dir():
        for entry in directory.iterdir():
            if entry.name.startswith(".enrollment-") and entry.name.endswith(".tmp"):
                try:
                    entry.unlink()
                except OSError:
                    _refuse("enrollment_state_permissions")
    return removed


def requires_reenrollment(state: Any) -> bool:
    """True whenever the live secret is gone — which is always, after a restart.

    There is deliberately no path from persisted state back to the secret, so
    this returns True for any state read from disk. It exists to make the
    fail-closed answer explicit at the call site rather than implied by the
    absence of an accessor.
    """

    if isinstance(state, EnrollmentSecret):
        return not state.available
    if isinstance(state, EnrollmentState):
        return True
    _refuse("enrollment_state_invalid")


# --------------------------------------------------------------------------- #
# Handoff — stdin only, never argv
# --------------------------------------------------------------------------- #


def ssh_handoff_argv(
    ssh_bin: str,
    host: str,
    remote_script: str,
    *,
    display: str,
    web_port: int,
    vnc_port: int,
    auth_mode: str,
) -> list[str]:
    """Build the host-start command. The secret is NOT one of the arguments.

    argv is world-readable through ``ps`` on most hosts, so the secret travels
    over stdin (``--password-stdin``) and this builder has no parameter that
    could carry it.
    """

    if type(ssh_bin) is not str or not ssh_bin:
        _refuse("enrollment_state_invalid")
    if type(host) is not str or HOST_PATTERN.fullmatch(host) is None:
        _refuse("enrollment_state_invalid")
    if type(remote_script) is not str or not remote_script:
        _refuse("enrollment_state_invalid")
    if type(display) is not str or re.fullmatch(r"^:[0-9]{1,3}$", display) is None:
        _refuse("enrollment_state_invalid")
    if type(auth_mode) is not str or re.fullmatch(r"^[a-z0-9-]{1,32}$", auth_mode) is None:
        _refuse("enrollment_state_invalid")
    _bounded_int(web_port, MIN_PORT, MAX_PORT, "enrollment_state_invalid")
    _bounded_int(vnc_port, MIN_PORT, MAX_PORT, "enrollment_state_invalid")

    argv = [
        ssh_bin,
        "-T",
        host,
        "--",
        remote_script,
        "host-start",
        "--display",
        display,
        "--web-port",
        str(web_port),
        "--vnc-port",
        str(vnc_port),
        "--auth-mode",
        auth_mode,
        "--password-stdin",
    ]
    assert_argv_secret_free(argv)
    return argv


def assert_argv_secret_free(argv: Any) -> None:
    """Refuse a command line carrying anything secret-shaped."""

    if not isinstance(argv, (list, tuple)):
        _refuse("enrollment_state_invalid")
    for entry in argv:
        if type(entry) is not str:
            _refuse("enrollment_state_invalid")
        if SECRET_PATTERN.fullmatch(entry) is not None:
            _refuse("secret_in_argv_forbidden")


def handoff_stdin_payload(secret: Any) -> bytes:
    """Render the one-time stdin handoff, consuming the disclosure.

    The only place in this module that touches the secret value, and it returns
    bytes destined for a pipe — never a file, never a log, never argv.
    """

    if not isinstance(secret, EnrollmentSecret):
        _refuse("secret_invalid")
    return (secret.disclose() + "\n").encode("utf-8")


__all__ = [
    "ENROLLMENT_STATE_REL_PATH",
    "ORACLE_ENROLLMENT_STATE_SCHEMA",
    "REFUSAL_CODES",
    "SECRET_ENTROPY_BYTES",
    "SECRET_PATTERN",
    "STATE_KEYS",
    "VERIFIER_ALGORITHM",
    "VERIFIER_ITERATIONS",
    "EnrollmentSecret",
    "EnrollmentState",
    "OracleEnrollmentError",
    "SecretVerifier",
    "assert_argv_secret_free",
    "assert_payload_secret_free",
    "clear_enrollment_state",
    "enrollment_state_path",
    "handoff_stdin_payload",
    "mint_enrollment_secret",
    "read_enrollment_state",
    "requires_reenrollment",
    "ssh_handoff_argv",
    "write_enrollment_state",
]
