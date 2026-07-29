"""Fail-closed caller policy and quota reservations for the Oracle broker.

The broker authenticates a transport peer and passes that identity here.  This
module deliberately accepts only size/count facts, never prompt text, file
paths, cookies, hooks, environment values, browser configuration, or CDP
targets. Persistent admission state is rooted in a separately provisioned local
authority; ordinary engine construction cannot create or reset that authority.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping

ORACLE_POLICY_SCHEMA = "skillbox.oracle-policy.v1"
ORACLE_REQUEST_FACTS_SCHEMA = "skillbox.oracle-request-facts.v1"
ORACLE_POLICY_STATE_SCHEMA = "skillbox.oracle-policy-state.v4"
ORACLE_POLICY_NAMESPACE_SCHEMA = "skillbox.oracle-policy-namespace.v3"
ORACLE_POLICY_AUTHORITY_SCHEMA = "skillbox.oracle-policy-authority.v1"
ORACLE_POLICY_AUTHORITY_HEAD_SCHEMA = "skillbox.oracle-policy-authority-head.v1"
ORACLE_POLICY_AUTHORITY_ENTRY_SCHEMA = "skillbox.oracle-policy-authority-entry.v1"

SUPPORTED_MODES = frozenset({"standard", "deep-research"})
CALLER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RESERVATION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_CALLER_POLICY_KEYS = frozenset(
    {
        "modes",
        "max_prompt_bytes",
        "max_files",
        "max_attachment_bytes",
        "max_request_bytes",
        "max_concurrent",
        "max_requests_per_window",
        "max_bytes_per_window",
        "window_seconds",
        "max_runtime_seconds",
        "lease_grace_seconds",
    }
)
_REQUEST_FACT_KEYS = frozenset(
    {
        "schema",
        "mode",
        "prompt_bytes",
        "file_count",
        "attachment_bytes",
        "timeout_seconds",
    }
)
_INTEGER_BOUNDS = {
    "max_prompt_bytes": (1, 4 * 1024 * 1024),
    "max_files": (0, 32),
    "max_attachment_bytes": (0, 256 * 1024 * 1024),
    "max_request_bytes": (1, 260 * 1024 * 1024),
    "max_concurrent": (1, 16),
    "max_requests_per_window": (1, 10_000),
    "max_bytes_per_window": (1, 10 * 1024 * 1024 * 1024),
    "window_seconds": (1, 86_400),
    "max_runtime_seconds": (1, 21_600),
    "lease_grace_seconds": (0, 600),
}
_MAX_CALLERS = 64
_MAX_STATE_BYTES = 2 * 1024 * 1024
_MAX_STATE_RECORDS = 100_000
_MAX_NAMESPACE_BYTES = 4 * 1024
_MAX_AUTHORITY_MANIFEST_BYTES = 8 * 1024
_MAX_AUTHORITY_HEAD_BYTES = 2 * 1024
_MAX_AUTHORITY_HISTORY_BYTES = 8 * 1024 * 1024
_MAX_AUTHORITY_ENTRIES = 100_000
_NAMESPACE_DIRECTORY_NAME = ".oracle-policy-namespaces"
_AUTHORITY_MANIFEST_NAME = "authority.json"
_AUTHORITY_HEAD_NAME = "authority-head.json"
_AUTHORITY_HISTORY_NAME = "authority-history.jsonl"


class OraclePolicyError(RuntimeError):
    """Stable, non-sensitive policy denial."""

    def __init__(self, code: str) -> None:
        super().__init__("oracle policy: denied")
        self.code = code


def _deny(code: str) -> None:
    raise OraclePolicyError(code)


def _secure_token_hex(byte_count: int, code: str) -> str:
    try:
        value = secrets.token_hex(byte_count)
    except Exception:
        _deny(code)
    if (
        type(value) is not str
        or len(value) != byte_count * 2
        or re.fullmatch(r"[0-9a-f]+", value) is None
    ):
        _deny(code)
    return value


def _exact_mapping(value: Any, keys: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != keys:
        _deny(code)
    return value


def _bounded_integer(value: Any, minimum: int, maximum: int, code: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _deny(code)
    return value


def _validate_caller_id(value: Any, code: str = "caller_id_invalid") -> str:
    if not isinstance(value, str) or CALLER_ID_PATTERN.fullmatch(value) is None:
        _deny(code)
    return value


def _strict_json_object(
    pairs: list[tuple[str, Any]],
    code: str = "state_corrupt",
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _deny(code)
        result[key] = value
    return result


@dataclass(frozen=True)
class CallerPolicy:
    modes: frozenset[str]
    max_prompt_bytes: int
    max_files: int
    max_attachment_bytes: int
    max_request_bytes: int
    max_concurrent: int
    max_requests_per_window: int
    max_bytes_per_window: int
    window_seconds: int
    max_runtime_seconds: int
    lease_grace_seconds: int

    @classmethod
    def from_mapping(cls, value: Any) -> CallerPolicy:
        raw = _exact_mapping(value, _CALLER_POLICY_KEYS, "policy_config_invalid")
        raw_modes = raw["modes"]
        if (
            not isinstance(raw_modes, list)
            or not raw_modes
            or any(not isinstance(mode, str) for mode in raw_modes)
            or len(raw_modes) != len(set(raw_modes))
            or any(mode not in SUPPORTED_MODES for mode in raw_modes)
        ):
            _deny("policy_config_invalid")
        limits = {
            key: _bounded_integer(
                raw[key],
                _INTEGER_BOUNDS[key][0],
                _INTEGER_BOUNDS[key][1],
                "policy_config_invalid",
            )
            for key in _INTEGER_BOUNDS
        }
        if limits["max_request_bytes"] < max(
            limits["max_prompt_bytes"],
            limits["max_attachment_bytes"],
        ):
            _deny("policy_config_invalid")
        if limits["max_bytes_per_window"] < limits["max_request_bytes"]:
            _deny("policy_config_invalid")
        return cls(modes=frozenset(raw_modes), **limits)


@dataclass(frozen=True)
class OraclePolicy:
    callers: Mapping[str, CallerPolicy]

    @classmethod
    def from_mapping(cls, value: Any) -> OraclePolicy:
        raw = _exact_mapping(
            value,
            frozenset({"schema", "callers"}),
            "policy_config_invalid",
        )
        if raw["schema"] != ORACLE_POLICY_SCHEMA:
            _deny("policy_config_invalid")
        raw_callers = raw["callers"]
        if (
            not isinstance(raw_callers, Mapping)
            or not raw_callers
            or len(raw_callers) > _MAX_CALLERS
        ):
            _deny("policy_config_invalid")
        callers: dict[str, CallerPolicy] = {}
        for caller_id, caller_policy in raw_callers.items():
            callers[_validate_caller_id(caller_id, "policy_config_invalid")] = (
                CallerPolicy.from_mapping(caller_policy)
            )
        return cls(callers=MappingProxyType(callers))


def _caller_policy_mapping(value: CallerPolicy) -> dict[str, Any]:
    try:
        return {
            "modes": sorted(value.modes),
            **{key: getattr(value, key) for key in _INTEGER_BOUNDS},
        }
    except (AttributeError, TypeError):
        _deny("policy_engine_invalid")


def _normalized_policy(value: Any) -> tuple[OraclePolicy, str]:
    if not isinstance(value, OraclePolicy) or not isinstance(value.callers, Mapping):
        _deny("policy_engine_invalid")
    try:
        document = {
            "schema": ORACLE_POLICY_SCHEMA,
            "callers": {
                caller_id: _caller_policy_mapping(caller_policy)
                for caller_id, caller_policy in value.callers.items()
            },
        }
    except (AttributeError, TypeError):
        _deny("policy_engine_invalid")
    try:
        normalized = OraclePolicy.from_mapping(document)
    except OraclePolicyError:
        _deny("policy_engine_invalid")
    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return normalized, hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class OracleRequestFacts:
    mode: str
    prompt_bytes: int
    file_count: int
    attachment_bytes: int
    timeout_seconds: int

    def __post_init__(self) -> None:
        if type(self.mode) is not str or self.mode not in SUPPORTED_MODES:
            _deny("request_shape_invalid")
        _bounded_integer(
            self.prompt_bytes,
            1,
            _INTEGER_BOUNDS["max_prompt_bytes"][1],
            "request_shape_invalid",
        )
        _bounded_integer(
            self.file_count,
            0,
            _INTEGER_BOUNDS["max_files"][1],
            "request_shape_invalid",
        )
        _bounded_integer(
            self.attachment_bytes,
            0,
            _INTEGER_BOUNDS["max_attachment_bytes"][1],
            "request_shape_invalid",
        )
        _bounded_integer(
            self.timeout_seconds,
            1,
            _INTEGER_BOUNDS["max_runtime_seconds"][1],
            "request_shape_invalid",
        )
        if (self.file_count == 0) != (self.attachment_bytes == 0):
            _deny("request_shape_invalid")

    @classmethod
    def from_mapping(cls, value: Any) -> OracleRequestFacts:
        raw = _exact_mapping(value, _REQUEST_FACT_KEYS, "request_shape_invalid")
        if raw["schema"] != ORACLE_REQUEST_FACTS_SCHEMA:
            _deny("request_shape_invalid")
        mode = raw["mode"]
        if type(mode) is not str or mode not in SUPPORTED_MODES:
            _deny("request_shape_invalid")
        prompt_bytes = _bounded_integer(
            raw["prompt_bytes"],
            1,
            _INTEGER_BOUNDS["max_prompt_bytes"][1],
            "request_shape_invalid",
        )
        file_count = _bounded_integer(
            raw["file_count"],
            0,
            _INTEGER_BOUNDS["max_files"][1],
            "request_shape_invalid",
        )
        attachment_bytes = _bounded_integer(
            raw["attachment_bytes"],
            0,
            _INTEGER_BOUNDS["max_attachment_bytes"][1],
            "request_shape_invalid",
        )
        timeout_seconds = _bounded_integer(
            raw["timeout_seconds"],
            1,
            _INTEGER_BOUNDS["max_runtime_seconds"][1],
            "request_shape_invalid",
        )
        if (file_count == 0) != (attachment_bytes == 0):
            _deny("request_shape_invalid")
        return cls(
            mode=mode,
            prompt_bytes=prompt_bytes,
            file_count=file_count,
            attachment_bytes=attachment_bytes,
            timeout_seconds=timeout_seconds,
        )

    @property
    def request_bytes(self) -> int:
        return self.prompt_bytes + self.attachment_bytes


@dataclass(frozen=True)
class PolicyGrant:
    caller_id: str
    reservation_id: str
    mode: str
    admitted_at: int
    expires_at: int
    request_bytes: int


def _validated_request_snapshot(
    value: Any,
) -> tuple[OracleRequestFacts, int]:
    if type(value) is not OracleRequestFacts:
        _deny("request_shape_invalid")
    try:
        mode = value.mode
        raw_prompt_bytes = value.prompt_bytes
        raw_file_count = value.file_count
        raw_attachment_bytes = value.attachment_bytes
        raw_timeout_seconds = value.timeout_seconds
    except (AttributeError, TypeError):
        _deny("request_shape_invalid")
    if type(mode) is not str or mode not in SUPPORTED_MODES:
        _deny("request_shape_invalid")
    prompt_bytes = _bounded_integer(
        raw_prompt_bytes,
        1,
        _INTEGER_BOUNDS["max_prompt_bytes"][1],
        "request_shape_invalid",
    )
    file_count = _bounded_integer(
        raw_file_count,
        0,
        _INTEGER_BOUNDS["max_files"][1],
        "request_shape_invalid",
    )
    attachment_bytes = _bounded_integer(
        raw_attachment_bytes,
        0,
        _INTEGER_BOUNDS["max_attachment_bytes"][1],
        "request_shape_invalid",
    )
    timeout_seconds = _bounded_integer(
        raw_timeout_seconds,
        1,
        _INTEGER_BOUNDS["max_runtime_seconds"][1],
        "request_shape_invalid",
    )
    if (file_count == 0) != (attachment_bytes == 0):
        _deny("request_shape_invalid")
    snapshot = OracleRequestFacts(
        mode=mode,
        prompt_bytes=prompt_bytes,
        file_count=file_count,
        attachment_bytes=attachment_bytes,
        timeout_seconds=timeout_seconds,
    )
    return snapshot, prompt_bytes + attachment_bytes


@dataclass(frozen=True)
class _StateHead:
    revision: int
    sha256: str


@dataclass(frozen=True)
class _NamespaceBinding:
    generation: str
    state_head: _StateHead | None
    pending_head: _StateHead | None


@dataclass(frozen=True)
class _AuthorityManifest:
    authority_generation: str
    namespace_generation: str
    authority_parent_identity: tuple[int, int]
    authority_identity: tuple[int, int]
    manifest_identity: tuple[int, int]
    head_identity: tuple[int, int]
    history_identity: tuple[int, int]
    state_parent_identity: tuple[int, int]
    anchor_identity: tuple[int, int]
    state_identity: tuple[int, int]
    namespace_identity: tuple[int, int]


@dataclass(frozen=True)
class _AuthorityEntry:
    sequence: int
    entry_hash: str
    history_size: int
    phase: str
    binding: _NamespaceBinding
    namespace_sha256: str


@dataclass
class _LockedAuthority:
    authority_parent_descriptor: int
    authority_descriptor: int
    parent_descriptor: int
    anchor_descriptor: int
    state_descriptor: int
    manifest: _AuthorityManifest
    authority_entry: _AuthorityEntry
    binding: _NamespaceBinding
    state_ctime_ns: int


def _verify_private_directory_metadata(
    metadata: os.stat_result,
    code: str = "state_directory_unsafe",
) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
    ):
        _deny(code)


def _verify_directory_metadata(
    metadata: os.stat_result,
    code: str = "state_directory_unsafe",
) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        _deny(code)


def _verify_trusted_parent_metadata(
    metadata: os.stat_result,
    code: str = "state_directory_unsafe",
) -> None:
    _verify_directory_metadata(metadata, code)
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        _deny(code)


def _directory_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _close_descriptors(descriptors: Iterator[int], code: str) -> None:
    failed = False
    for descriptor in descriptors:
        if descriptor < 0:
            continue
        try:
            os.close(descriptor)
        except Exception:
            failed = True
    if failed:
        _deny(code)


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_directory_component(
    parent_descriptor: int,
    name: str,
    *,
    create: bool,
    private: bool,
    code: str = "state_directory_unsafe",
) -> tuple[int, os.stat_result]:
    created = False
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except FileNotFoundError:
        if not create:
            _deny(code)
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
            created = True
        except FileExistsError:
            pass
        except (OSError, ValueError):
            _deny(code)
        try:
            descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
        except (OSError, ValueError):
            _deny(code)
        if created:
            try:
                os.fsync(parent_descriptor)
            except (OSError, ValueError):
                _close_descriptors(iter((descriptor,)), code)
                _deny(code)
    except (OSError, ValueError):
        _deny(code)
    verified = False
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        verifier = (
            _verify_private_directory_metadata
            if private
            else _verify_directory_metadata
        )
        verifier(opened, code)
        verifier(named, code)
        if _directory_identity(opened) != _directory_identity(named):
            _deny(code)
        if created and stat.S_IMODE(opened.st_mode) != 0o700:
            _deny(code)
        verified = True
        return descriptor, opened
    except (OSError, ValueError):
        _deny(code)
    finally:
        if not verified:
            _close_descriptors(iter((descriptor,)), code)


def _create_private_directory_component(
    parent_descriptor: int,
    name: str,
    *,
    code: str,
    exists_code: str,
) -> tuple[int, os.stat_result]:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except FileExistsError:
        _deny(exists_code)
    except (OSError, ValueError):
        _deny(code)
    return _open_directory_component(
        parent_descriptor,
        name,
        create=False,
        private=True,
        code=code,
    )


def _canonical_directory_path(
    value: str | Path,
    *,
    code: str,
) -> Path:
    try:
        raw_path = os.fspath(value)
    except Exception:
        _deny(code)
    if (
        type(raw_path) is not str
        or not raw_path
        or "\x00" in raw_path
        or any("\ud800" <= character <= "\udfff" for character in raw_path)
    ):
        _deny(code)
    try:
        normalized_path = os.path.normpath(raw_path)
    except Exception:
        _deny(code)
    raw_parts = raw_path.split(os.sep)
    if (
        not raw_path.startswith(os.sep)
        or raw_path == os.sep
        or raw_path.endswith(os.sep)
        or f"{os.sep}{os.sep}" in raw_path
        or any(part in {".", ".."} for part in raw_parts)
        or normalized_path != raw_path
    ):
        _deny(code)
    try:
        path = Path(raw_path)
        encoded_path = os.fsencode(raw_path)
    except Exception:
        _deny(code)
    if (
        not path.is_absolute()
        or path.anchor != os.sep
        or path == Path(os.sep)
        or os.fspath(path) != raw_path
        or not encoded_path
    ):
        _deny(code)
    return path


def _canonical_state_path(value: str | Path) -> Path:
    path = _canonical_directory_path(value, code="state_directory_unsafe")
    if path.name == _NAMESPACE_DIRECTORY_NAME:
        _deny("state_directory_unsafe")
    return path


def _canonical_authority_path(value: str | Path) -> Path:
    return _canonical_directory_path(value, code="authority_directory_unsafe")


def _assert_separate_authority_paths(
    state_directory: Path,
    authority_directory: Path,
) -> None:
    state_anchor = state_directory.parent / _NAMESPACE_DIRECTORY_NAME
    state_parts = state_directory.parts
    anchor_parts = state_anchor.parts
    authority_parts = authority_directory.parts
    if (
        authority_directory == state_anchor
        or authority_parts[: len(state_parts)] == state_parts
        or state_parts[: len(authority_parts)] == authority_parts
        or authority_parts[: len(anchor_parts)] == anchor_parts
        or anchor_parts[: len(authority_parts)] == authority_parts
    ):
        _deny("authority_directory_unsafe")


def _open_parent_descriptor(
    state_directory: Path,
    *,
    create: bool,
    code: str = "state_directory_unsafe",
) -> tuple[int, os.stat_result]:
    components = state_directory.parts[1:-1]
    try:
        descriptor = os.open(os.sep, _directory_flags())
    except (OSError, ValueError):
        _deny(code)
    try:
        for component in components:
            next_descriptor, _metadata = _open_directory_component(
                descriptor,
                component,
                create=create,
                private=False,
                code=code,
            )
            _close_descriptors(iter((descriptor,)), code)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        _verify_trusted_parent_metadata(metadata, code)
        return descriptor, metadata
    except OraclePolicyError:
        _close_descriptors(iter((descriptor,)), code)
        raise
    except (OSError, ValueError):
        _close_descriptors(iter((descriptor,)), code)
        _deny(code)


def _verify_private_file(metadata: os.stat_result, code: str) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
    ):
        _deny(code)


def _read_private_bytes(
    directory_descriptor: int,
    name: str,
    *,
    maximum_bytes: int,
    code: str,
    expected_identity: tuple[int, int] | None = None,
) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        _deny(code)
    try:
        metadata = os.fstat(descriptor)
        _verify_private_file(metadata, code)
        try:
            named = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except (OSError, ValueError):
            _deny(code)
        _verify_private_file(named, code)
        if _directory_identity(metadata) != _directory_identity(named):
            _deny(code)
        if (
            expected_identity is not None
            and _directory_identity(metadata) != expected_identity
        ):
            _deny(code)
        if metadata.st_size > maximum_bytes:
            _deny(code)
        try:
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
        except (OSError, ValueError):
            _deny(code)
    except (OSError, ValueError):
        _deny(code)
    finally:
        if descriptor >= 0:
            _close_descriptors(iter((descriptor,)), code)
    if len(payload) > maximum_bytes:
        _deny(code)
    return payload


def _read_private_json(
    directory_descriptor: int,
    name: str,
    *,
    maximum_bytes: int,
    code: str,
    content_code: str | None = None,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[Any, bytes] | None:
    decode_code = content_code or code
    payload = _read_private_bytes(
        directory_descriptor,
        name,
        maximum_bytes=maximum_bytes,
        code=code,
        expected_identity=expected_identity,
    )
    if payload is None:
        return None
    return _decode_canonical_json_bytes(payload, decode_code), payload


def _decode_canonical_json_bytes(payload: bytes, code: str) -> Any:
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            parse_constant=lambda _value: _deny(code),
            object_pairs_hook=lambda pairs: _strict_json_object(pairs, code),
        )
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError):
        _deny(code)
    if payload != _canonical_json_bytes(decoded, code):
        _deny(code)
    return decoded


def _canonical_json_bytes(value: Any, code: str = "state_corrupt") -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode("ascii")
    except (RecursionError, TypeError, UnicodeError, ValueError):
        _deny(code)


def _atomic_write_private_file(
    directory_descriptor: int,
    name: str,
    payload: bytes,
    *,
    replace: bool,
    code: str,
) -> None:
    temporary_name = f".{name}.{_secure_token_hex(16, code)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
    except (OSError, ValueError):
        _deny(code)
    temporary_exists = True
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            try:
                current = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except (OSError, ValueError):
                _deny(code)
            _verify_private_file(current, code)
            try:
                os.replace(
                    temporary_name,
                    name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
            except (OSError, ValueError):
                _deny(code)
            temporary_exists = False
        else:
            try:
                os.link(
                    temporary_name,
                    name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except (OSError, ValueError):
                _deny(code)
            temporary_exists = False
        try:
            published = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            _verify_private_file(published, code)
            os.fsync(directory_descriptor)
        except (OSError, ValueError):
            _deny(code)
    except (OSError, ValueError):
        _deny(code)
    finally:
        if descriptor >= 0:
            _close_descriptors(iter((descriptor,)), code)
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                _deny(code)


def _create_empty_private_file(
    directory_descriptor: int,
    name: str,
    *,
    code: str,
) -> tuple[int, int]:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_descriptor)
    except FileExistsError:
        _deny("authority_already_enrolled")
    except (OSError, ValueError):
        _deny(code)
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        _verify_private_file(metadata, code)
        os.fsync(descriptor)
        os.fsync(directory_descriptor)
        named = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        _verify_private_file(named, code)
        if _directory_identity(metadata) != _directory_identity(named):
            _deny(code)
        return _directory_identity(metadata)
    except (OSError, ValueError):
        _deny(code)
    finally:
        _close_descriptors(iter((descriptor,)), code)


def _write_all(descriptor: int, payload: bytes, code: str) -> None:
    offset = 0
    try:
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                _deny(code)
            offset += written
    except (OSError, ValueError):
        _deny(code)


def _rewrite_private_file(
    directory_descriptor: int,
    name: str,
    payload: bytes,
    *,
    expected_identity: tuple[int, int],
    maximum_bytes: int,
    code: str,
) -> None:
    if len(payload) > maximum_bytes:
        _deny(code)
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except (OSError, ValueError):
        _deny(code)
    try:
        metadata = os.fstat(descriptor)
        _verify_private_file(metadata, code)
        if _directory_identity(metadata) != expected_identity:
            _deny(code)
        os.lseek(descriptor, 0, os.SEEK_SET)
        _write_all(descriptor, payload, code)
        os.ftruncate(descriptor, len(payload))
        os.fsync(descriptor)
        named = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        _verify_private_file(named, code)
        if _directory_identity(named) != expected_identity:
            _deny(code)
        os.fsync(directory_descriptor)
    except (OSError, ValueError):
        _deny(code)
    finally:
        _close_descriptors(iter((descriptor,)), code)


def _append_private_file(
    directory_descriptor: int,
    name: str,
    payload: bytes,
    *,
    expected_identity: tuple[int, int],
    expected_size: int,
    maximum_bytes: int,
    code: str,
) -> int:
    if expected_size < 0 or expected_size + len(payload) > maximum_bytes:
        _deny(code)
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except (OSError, ValueError):
        _deny(code)
    try:
        metadata = os.fstat(descriptor)
        _verify_private_file(metadata, code)
        if (
            _directory_identity(metadata) != expected_identity
            or metadata.st_size != expected_size
        ):
            _deny(code)
        _write_all(descriptor, payload, code)
        os.fsync(descriptor)
        published = os.fstat(descriptor)
        named = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        _verify_private_file(published, code)
        _verify_private_file(named, code)
        if (
            _directory_identity(published) != expected_identity
            or _directory_identity(named) != expected_identity
            or published.st_size != expected_size + len(payload)
        ):
            _deny(code)
        os.fsync(directory_descriptor)
        return published.st_size
    except (OSError, ValueError):
        _deny(code)
    finally:
        _close_descriptors(iter((descriptor,)), code)


def _initial_state(
    policy_fingerprint: str,
    namespace_generation: str,
) -> dict[str, Any]:
    return {
        "schema": ORACLE_POLICY_STATE_SCHEMA,
        "policy_fingerprint": policy_fingerprint,
        "namespace_generation": namespace_generation,
        "revision": 0,
        "last_seen_at": 0,
        "callers": {},
    }


def _parse_state_head(value: Any, code: str) -> _StateHead | None:
    if value is None:
        return None
    head = _exact_mapping(
        value,
        frozenset({"revision", "sha256"}),
        code,
    )
    revision = _bounded_integer(
        head["revision"],
        1,
        2**63 - 1,
        code,
    )
    digest = head["sha256"]
    if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
        _deny(code)
    return _StateHead(revision=revision, sha256=digest)


def _head_document(head: _StateHead | None) -> dict[str, Any] | None:
    if head is None:
        return None
    return {
        "revision": head.revision,
        "sha256": head.sha256,
    }


def _validate_state(
    state_value: Any,
    policy_fingerprint: str,
    namespace_generation: str,
    expected_revision: int,
) -> dict[str, Any]:
    state = _exact_mapping(
        state_value,
        frozenset(
            {
                "schema",
                "policy_fingerprint",
                "namespace_generation",
                "revision",
                "last_seen_at",
                "callers",
            }
        ),
        "state_corrupt",
    )
    if state["schema"] != ORACLE_POLICY_STATE_SCHEMA:
        _deny("state_corrupt")
    if (
        not isinstance(state["policy_fingerprint"], str)
        or SHA256_PATTERN.fullmatch(state["policy_fingerprint"]) is None
    ):
        _deny("state_corrupt")
    if state["policy_fingerprint"] != policy_fingerprint:
        _deny("policy_state_mismatch")
    if (
        not isinstance(state["namespace_generation"], str)
        or SHA256_PATTERN.fullmatch(state["namespace_generation"]) is None
    ):
        _deny("state_corrupt")
    if state["namespace_generation"] != namespace_generation:
        _deny("state_directory_unsafe")
    revision = _bounded_integer(
        state["revision"],
        1,
        2**63 - 1,
        "state_corrupt",
    )
    if revision != expected_revision:
        _deny("state_corrupt")
    last_seen_at = _bounded_integer(
        state["last_seen_at"], 0, 2**63 - 1, "state_corrupt"
    )
    callers = state["callers"]
    if not isinstance(callers, dict) or len(callers) > _MAX_CALLERS * 4:
        _deny("state_corrupt")
    record_count = 0
    for caller_id, bucket_value in callers.items():
        _validate_caller_id(caller_id, "state_corrupt")
        bucket = _exact_mapping(
            bucket_value,
            frozenset({"events", "reservations"}),
            "state_corrupt",
        )
        events = bucket["events"]
        reservations = bucket["reservations"]
        if not isinstance(events, list) or not isinstance(reservations, dict):
            _deny("state_corrupt")
        record_count += len(events) + len(reservations)
        previous_at = 0
        for event_value in events:
            event = _exact_mapping(
                event_value, frozenset({"at", "bytes"}), "state_corrupt"
            )
            event_at = _bounded_integer(event["at"], 0, last_seen_at, "state_corrupt")
            _bounded_integer(event["bytes"], 1, 2**63 - 1, "state_corrupt")
            if event_at < previous_at:
                _deny("state_corrupt")
            previous_at = event_at
        for reservation_id, reservation_value in reservations.items():
            if (
                not isinstance(reservation_id, str)
                or RESERVATION_ID_PATTERN.fullmatch(reservation_id) is None
            ):
                _deny("state_corrupt")
            reservation = _exact_mapping(
                reservation_value,
                frozenset({"mode", "admitted_at", "expires_at", "request_bytes"}),
                "state_corrupt",
            )
            if (
                not isinstance(reservation["mode"], str)
                or reservation["mode"] not in SUPPORTED_MODES
            ):
                _deny("state_corrupt")
            admitted_at = _bounded_integer(
                reservation["admitted_at"], 0, last_seen_at, "state_corrupt"
            )
            _bounded_integer(
                reservation["expires_at"], admitted_at, 2**63 - 1, "state_corrupt"
            )
            _bounded_integer(
                reservation["request_bytes"], 1, 2**63 - 1, "state_corrupt"
            )
    if record_count > _MAX_STATE_RECORDS:
        _deny("state_corrupt")
    return state_value


class OraclePolicyEngine:
    """Cross-process admission control rooted in a pre-enrolled authority."""

    def __init__(
        self,
        policy: OraclePolicy,
        state_directory: str | Path,
        *,
        authority_directory: str | Path,
        clock: Callable[[], float] = time.time,
        lock_timeout_seconds: float = 2.0,
    ) -> None:
        self._configure(
            policy,
            state_directory,
            authority_directory,
            clock,
            lock_timeout_seconds,
        )
        self._attach_existing_authority()

    @classmethod
    def _provision(
        cls,
        policy: OraclePolicy,
        state_directory: str | Path,
        authority_directory: str | Path,
        lock_timeout_seconds: float,
    ) -> None:
        engine = cls.__new__(cls)
        engine._configure(
            policy,
            state_directory,
            authority_directory,
            time.time,
            lock_timeout_seconds,
        )
        engine._enroll_authority()

    def _configure(
        self,
        policy: OraclePolicy,
        state_directory: str | Path,
        authority_directory: str | Path,
        clock: Callable[[], float],
        lock_timeout_seconds: float,
    ) -> None:
        if not callable(clock):
            _deny("policy_engine_invalid")
        normalized_policy, policy_fingerprint = _normalized_policy(policy)
        if (
            isinstance(lock_timeout_seconds, bool)
            or not isinstance(lock_timeout_seconds, (int, float))
            or not math.isfinite(lock_timeout_seconds)
            or not 0 < lock_timeout_seconds <= 30
        ):
            _deny("policy_engine_invalid")
        self.policy = normalized_policy
        self.policy_fingerprint = policy_fingerprint
        self.state_directory = _canonical_state_path(state_directory)
        self.authority_directory = _canonical_authority_path(authority_directory)
        _assert_separate_authority_paths(
            self.state_directory,
            self.authority_directory,
        )
        self.state_path = self.state_directory / "policy-state.json"
        try:
            encoded_state_path = os.fsencode(self.state_directory)
        except (OSError, TypeError, UnicodeError, ValueError):
            _deny("state_directory_unsafe")
        path_digest = hashlib.sha256(encoded_state_path).hexdigest()
        self._namespace_name = f"{path_digest}.json"
        self.namespace_path = (
            self.state_directory.parent
            / _NAMESPACE_DIRECTORY_NAME
            / self._namespace_name
        )
        self.authority_manifest_path = (
            self.authority_directory / _AUTHORITY_MANIFEST_NAME
        )
        self.authority_head_path = self.authority_directory / _AUTHORITY_HEAD_NAME
        self.authority_history_path = self.authority_directory / _AUTHORITY_HISTORY_NAME
        self._clock = clock
        self._lock_timeout_seconds = float(lock_timeout_seconds)
        self._authority_floor_sequence = -1
        self._authority_floor_hash = ""

    @staticmethod
    def _monotonic() -> float:
        try:
            value = time.monotonic()
        except Exception:
            _deny("state_lock_failed")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            _deny("state_lock_failed")
        return float(value)

    def _acquire_lock(self, descriptor: int) -> None:
        deadline = self._monotonic() + self._lock_timeout_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if self._monotonic() >= deadline:
                    _deny("state_lock_timeout")
                try:
                    time.sleep(0.01)
                except Exception:
                    _deny("state_lock_failed")
            except (OSError, ValueError):
                _deny("state_lock_failed")

    @staticmethod
    def _unlock(descriptor: int) -> None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except (OSError, ValueError):
            _deny("state_lock_failed")

    def _open_authority_domain(
        self,
    ) -> tuple[int, os.stat_result, int, os.stat_result]:
        parent_descriptor, parent_metadata = _open_parent_descriptor(
            self.authority_directory,
            create=False,
            code="authority_directory_unsafe",
        )
        authority_descriptor = -1
        try:
            authority_descriptor, authority_metadata = _open_directory_component(
                parent_descriptor,
                self.authority_directory.name,
                create=False,
                private=True,
                code="authority_directory_unsafe",
            )
            return (
                parent_descriptor,
                parent_metadata,
                authority_descriptor,
                authority_metadata,
            )
        except OraclePolicyError:
            _close_descriptors(
                iter((authority_descriptor, parent_descriptor)),
                "authority_directory_unsafe",
            )
            raise
        except (OSError, ValueError):
            _close_descriptors(
                iter((authority_descriptor, parent_descriptor)),
                "authority_directory_unsafe",
            )
            _deny("authority_directory_unsafe")

    @staticmethod
    def _private_file_identity(
        directory_descriptor: int,
        name: str,
        code: str,
    ) -> tuple[int, int]:
        try:
            metadata = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except (OSError, ValueError):
            _deny(code)
        _verify_private_file(metadata, code)
        return _directory_identity(metadata)

    def _manifest_document(
        self,
        manifest: _AuthorityManifest,
    ) -> dict[str, Any]:
        return {
            "schema": ORACLE_POLICY_AUTHORITY_SCHEMA,
            "policy_fingerprint": self.policy_fingerprint,
            "authority_generation": manifest.authority_generation,
            "namespace_generation": manifest.namespace_generation,
            "authority_directory": os.fspath(self.authority_directory),
            "state_directory": os.fspath(self.state_directory),
            "namespace_name": self._namespace_name,
            "authority_parent_device": manifest.authority_parent_identity[0],
            "authority_parent_inode": manifest.authority_parent_identity[1],
            "authority_device": manifest.authority_identity[0],
            "authority_inode": manifest.authority_identity[1],
            "manifest_device": manifest.manifest_identity[0],
            "manifest_inode": manifest.manifest_identity[1],
            "head_device": manifest.head_identity[0],
            "head_inode": manifest.head_identity[1],
            "history_device": manifest.history_identity[0],
            "history_inode": manifest.history_identity[1],
            "state_parent_device": manifest.state_parent_identity[0],
            "state_parent_inode": manifest.state_parent_identity[1],
            "anchor_device": manifest.anchor_identity[0],
            "anchor_inode": manifest.anchor_identity[1],
            "state_device": manifest.state_identity[0],
            "state_inode": manifest.state_identity[1],
            "namespace_device": manifest.namespace_identity[0],
            "namespace_inode": manifest.namespace_identity[1],
        }

    def _validate_manifest(
        self,
        value: Any,
        *,
        authority_parent_identity: tuple[int, int],
        authority_identity: tuple[int, int],
        manifest_identity: tuple[int, int],
        head_identity: tuple[int, int],
        history_identity: tuple[int, int],
    ) -> _AuthorityManifest:
        keys = {
            "schema",
            "policy_fingerprint",
            "authority_generation",
            "namespace_generation",
            "authority_directory",
            "state_directory",
            "namespace_name",
        }
        identity_names = (
            "authority_parent",
            "authority",
            "manifest",
            "head",
            "history",
            "state_parent",
            "anchor",
            "state",
            "namespace",
        )
        for name in identity_names:
            keys.add(f"{name}_device")
            keys.add(f"{name}_inode")
        manifest_value = _exact_mapping(
            value,
            frozenset(keys),
            "authority_corrupt",
        )
        if manifest_value["schema"] != ORACLE_POLICY_AUTHORITY_SCHEMA:
            _deny("authority_corrupt")
        fingerprint = manifest_value["policy_fingerprint"]
        if (
            not isinstance(fingerprint, str)
            or SHA256_PATTERN.fullmatch(fingerprint) is None
        ):
            _deny("authority_corrupt")
        if fingerprint != self.policy_fingerprint:
            _deny("policy_state_mismatch")
        authority_generation = manifest_value["authority_generation"]
        namespace_generation = manifest_value["namespace_generation"]
        if (
            not isinstance(authority_generation, str)
            or SHA256_PATTERN.fullmatch(authority_generation) is None
            or not isinstance(namespace_generation, str)
            or SHA256_PATTERN.fullmatch(namespace_generation) is None
        ):
            _deny("authority_corrupt")
        if (
            manifest_value["authority_directory"] != os.fspath(self.authority_directory)
            or manifest_value["state_directory"] != os.fspath(self.state_directory)
            or manifest_value["namespace_name"] != self._namespace_name
        ):
            _deny("authority_directory_unsafe")

        def identity(name: str) -> tuple[int, int]:
            device = manifest_value[f"{name}_device"]
            inode = manifest_value[f"{name}_inode"]
            if (
                type(device) is not int
                or device < 0
                or type(inode) is not int
                or inode < 0
            ):
                _deny("authority_corrupt")
            return device, inode

        manifest = _AuthorityManifest(
            authority_generation=authority_generation,
            namespace_generation=namespace_generation,
            authority_parent_identity=identity("authority_parent"),
            authority_identity=identity("authority"),
            manifest_identity=identity("manifest"),
            head_identity=identity("head"),
            history_identity=identity("history"),
            state_parent_identity=identity("state_parent"),
            anchor_identity=identity("anchor"),
            state_identity=identity("state"),
            namespace_identity=identity("namespace"),
        )
        if (
            manifest.authority_parent_identity != authority_parent_identity
            or manifest.authority_identity != authority_identity
            or manifest.manifest_identity != manifest_identity
            or manifest.head_identity != head_identity
            or manifest.history_identity != history_identity
        ):
            _deny("authority_directory_unsafe")
        return manifest

    def _load_manifest(
        self,
        authority_parent_descriptor: int,
        authority_descriptor: int,
    ) -> _AuthorityManifest:
        try:
            authority_parent_metadata = os.fstat(authority_parent_descriptor)
            authority_metadata = os.fstat(authority_descriptor)
        except (OSError, ValueError):
            _deny("authority_directory_unsafe")
        _verify_trusted_parent_metadata(
            authority_parent_metadata,
            "authority_directory_unsafe",
        )
        _verify_private_directory_metadata(
            authority_metadata,
            "authority_directory_unsafe",
        )
        manifest_identity = self._private_file_identity(
            authority_descriptor,
            _AUTHORITY_MANIFEST_NAME,
            "authority_file_unsafe",
        )
        head_identity = self._private_file_identity(
            authority_descriptor,
            _AUTHORITY_HEAD_NAME,
            "authority_file_unsafe",
        )
        history_identity = self._private_file_identity(
            authority_descriptor,
            _AUTHORITY_HISTORY_NAME,
            "authority_file_unsafe",
        )
        manifest_document = _read_private_json(
            authority_descriptor,
            _AUTHORITY_MANIFEST_NAME,
            maximum_bytes=_MAX_AUTHORITY_MANIFEST_BYTES,
            code="authority_file_unsafe",
            content_code="authority_corrupt",
            expected_identity=manifest_identity,
        )
        if manifest_document is None:
            _deny("authority_file_unsafe")
        return self._validate_manifest(
            manifest_document[0],
            authority_parent_identity=_directory_identity(authority_parent_metadata),
            authority_identity=_directory_identity(authority_metadata),
            manifest_identity=manifest_identity,
            head_identity=head_identity,
            history_identity=history_identity,
        )

    def _namespace_document(
        self,
        binding: _NamespaceBinding,
        authority_sequence: int,
        manifest: _AuthorityManifest,
    ) -> dict[str, Any]:
        return {
            "schema": ORACLE_POLICY_NAMESPACE_SCHEMA,
            "policy_fingerprint": self.policy_fingerprint,
            "authority_generation": manifest.authority_generation,
            "authority_sequence": authority_sequence,
            "generation": binding.generation,
            "state_head": _head_document(binding.state_head),
            "pending_head": _head_document(binding.pending_head),
            "state_directory": os.fspath(self.state_directory),
            "parent_device": manifest.state_parent_identity[0],
            "parent_inode": manifest.state_parent_identity[1],
            "anchor_device": manifest.anchor_identity[0],
            "anchor_inode": manifest.anchor_identity[1],
            "state_device": manifest.state_identity[0],
            "state_inode": manifest.state_identity[1],
        }

    def _namespace_payload(
        self,
        binding: _NamespaceBinding,
        authority_sequence: int,
        manifest: _AuthorityManifest,
    ) -> bytes:
        payload = _canonical_json_bytes(
            self._namespace_document(
                binding,
                authority_sequence,
                manifest,
            ),
            "state_directory_unsafe",
        )
        if len(payload) > _MAX_NAMESPACE_BYTES:
            _deny("state_directory_unsafe")
        return payload

    def _validate_namespace(
        self,
        value: Any,
        *,
        authority_sequence: int,
        manifest: _AuthorityManifest,
    ) -> _NamespaceBinding:
        namespace = _exact_mapping(
            value,
            frozenset(
                {
                    "schema",
                    "policy_fingerprint",
                    "authority_generation",
                    "authority_sequence",
                    "generation",
                    "state_head",
                    "pending_head",
                    "state_directory",
                    "parent_device",
                    "parent_inode",
                    "anchor_device",
                    "anchor_inode",
                    "state_device",
                    "state_inode",
                }
            ),
            "state_directory_unsafe",
        )
        if namespace["schema"] != ORACLE_POLICY_NAMESPACE_SCHEMA:
            _deny("state_directory_unsafe")
        if namespace["policy_fingerprint"] != self.policy_fingerprint:
            _deny("policy_state_mismatch")
        if (
            namespace["authority_generation"] != manifest.authority_generation
            or namespace["authority_sequence"] != authority_sequence
            or namespace["generation"] != manifest.namespace_generation
            or namespace["state_directory"] != os.fspath(self.state_directory)
        ):
            _deny("state_directory_unsafe")
        if type(namespace["authority_sequence"]) is not int:
            _deny("state_directory_unsafe")
        state_head = _parse_state_head(
            namespace["state_head"],
            "state_directory_unsafe",
        )
        pending_head = _parse_state_head(
            namespace["pending_head"],
            "state_directory_unsafe",
        )
        if pending_head is not None:
            committed_revision = 0 if state_head is None else state_head.revision
            if pending_head.revision != committed_revision + 1:
                _deny("state_directory_unsafe")
        expected_identities = {
            "parent_device": manifest.state_parent_identity[0],
            "parent_inode": manifest.state_parent_identity[1],
            "anchor_device": manifest.anchor_identity[0],
            "anchor_inode": manifest.anchor_identity[1],
            "state_device": manifest.state_identity[0],
            "state_inode": manifest.state_identity[1],
        }
        for key, expected in expected_identities.items():
            if (
                type(namespace[key]) is not int
                or namespace[key] < 0
                or namespace[key] != expected
            ):
                _deny("state_directory_unsafe")
        return _NamespaceBinding(
            generation=namespace["generation"],
            state_head=state_head,
            pending_head=pending_head,
        )

    def _authority_entry_document(
        self,
        *,
        sequence: int,
        previous_hash: str,
        phase: str,
        binding: _NamespaceBinding,
        namespace_sha256: str,
        manifest: _AuthorityManifest,
    ) -> dict[str, Any]:
        return {
            "schema": ORACLE_POLICY_AUTHORITY_ENTRY_SCHEMA,
            "sequence": sequence,
            "previous_hash": previous_hash,
            "phase": phase,
            "authority_generation": manifest.authority_generation,
            "namespace_generation": manifest.namespace_generation,
            "state_head": _head_document(binding.state_head),
            "pending_head": _head_document(binding.pending_head),
            "namespace_sha256": namespace_sha256,
        }

    def _authority_head_document(
        self,
        entry: _AuthorityEntry,
        manifest: _AuthorityManifest,
    ) -> dict[str, Any]:
        return {
            "schema": ORACLE_POLICY_AUTHORITY_HEAD_SCHEMA,
            "authority_generation": manifest.authority_generation,
            "sequence": entry.sequence,
            "entry_hash": entry.entry_hash,
            "history_size": entry.history_size,
        }

    def _parse_authority_entry(
        self,
        value: Any,
        *,
        raw_entry: bytes,
        history_size: int,
        expected_sequence: int,
        expected_previous_hash: str,
        manifest: _AuthorityManifest,
    ) -> _AuthorityEntry:
        entry = _exact_mapping(
            value,
            frozenset(
                {
                    "schema",
                    "sequence",
                    "previous_hash",
                    "phase",
                    "authority_generation",
                    "namespace_generation",
                    "state_head",
                    "pending_head",
                    "namespace_sha256",
                }
            ),
            "authority_corrupt",
        )
        if (
            entry["schema"] != ORACLE_POLICY_AUTHORITY_ENTRY_SCHEMA
            or entry["sequence"] != expected_sequence
            or type(entry["sequence"]) is not int
            or entry["previous_hash"] != expected_previous_hash
            or entry["authority_generation"] != manifest.authority_generation
            or entry["namespace_generation"] != manifest.namespace_generation
        ):
            _deny("authority_corrupt")
        phase = entry["phase"]
        if phase not in {"committed", "pending"} or type(phase) is not str:
            _deny("authority_corrupt")
        namespace_sha256 = entry["namespace_sha256"]
        if (
            not isinstance(namespace_sha256, str)
            or SHA256_PATTERN.fullmatch(namespace_sha256) is None
        ):
            _deny("authority_corrupt")
        state_head = _parse_state_head(entry["state_head"], "authority_corrupt")
        pending_head = _parse_state_head(
            entry["pending_head"],
            "authority_corrupt",
        )
        binding = _NamespaceBinding(
            generation=manifest.namespace_generation,
            state_head=state_head,
            pending_head=pending_head,
        )
        return _AuthorityEntry(
            sequence=expected_sequence,
            entry_hash=hashlib.sha256(raw_entry).hexdigest(),
            history_size=history_size,
            phase=phase,
            binding=binding,
            namespace_sha256=namespace_sha256,
        )

    def _validate_authority_transition(
        self,
        previous: _AuthorityEntry | None,
        current: _AuthorityEntry,
    ) -> None:
        if previous is None:
            if (
                current.sequence != 0
                or current.phase != "committed"
                or current.binding.state_head is not None
                or current.binding.pending_head is not None
            ):
                _deny("authority_corrupt")
            return
        if current.phase == "pending":
            committed_revision = (
                0
                if previous.binding.state_head is None
                else previous.binding.state_head.revision
            )
            if (
                previous.phase != "committed"
                or current.binding.state_head != previous.binding.state_head
                or current.binding.pending_head is None
                or current.binding.pending_head.revision != committed_revision + 1
            ):
                _deny("authority_corrupt")
        elif (
            previous.phase != "pending"
            or previous.binding.pending_head is None
            or current.binding.state_head != previous.binding.pending_head
            or current.binding.pending_head is not None
        ):
            _deny("authority_corrupt")

    def _load_authority_history(
        self,
        authority_descriptor: int,
        manifest: _AuthorityManifest,
    ) -> _AuthorityEntry:
        history_payload = _read_private_bytes(
            authority_descriptor,
            _AUTHORITY_HISTORY_NAME,
            maximum_bytes=_MAX_AUTHORITY_HISTORY_BYTES,
            code="authority_file_unsafe",
            expected_identity=manifest.history_identity,
        )
        if not history_payload or not history_payload.endswith(b"\n"):
            _deny("authority_corrupt")
        raw_entries = history_payload.splitlines(keepends=True)
        if len(raw_entries) > _MAX_AUTHORITY_ENTRIES:
            _deny("authority_corrupt")
        previous: _AuthorityEntry | None = None
        previous_hash = "0" * 64
        history_size = 0
        entries: list[_AuthorityEntry] = []
        for sequence, raw_entry in enumerate(raw_entries):
            history_size += len(raw_entry)
            decoded = _decode_canonical_json_bytes(
                raw_entry,
                "authority_corrupt",
            )
            current = self._parse_authority_entry(
                decoded,
                raw_entry=raw_entry,
                history_size=history_size,
                expected_sequence=sequence,
                expected_previous_hash=previous_hash,
                manifest=manifest,
            )
            self._validate_authority_transition(previous, current)
            entries.append(current)
            previous = current
            previous_hash = current.entry_hash
        if previous is None:
            _deny("authority_corrupt")
        head_document = _read_private_json(
            authority_descriptor,
            _AUTHORITY_HEAD_NAME,
            maximum_bytes=_MAX_AUTHORITY_HEAD_BYTES,
            code="authority_file_unsafe",
            content_code="authority_corrupt",
            expected_identity=manifest.head_identity,
        )
        if head_document is None:
            _deny("authority_file_unsafe")
        head = _exact_mapping(
            head_document[0],
            frozenset(
                {
                    "schema",
                    "authority_generation",
                    "sequence",
                    "entry_hash",
                    "history_size",
                }
            ),
            "authority_corrupt",
        )
        if (
            head["schema"] != ORACLE_POLICY_AUTHORITY_HEAD_SCHEMA
            or head["authority_generation"] != manifest.authority_generation
            or head["sequence"] != previous.sequence
            or type(head["sequence"]) is not int
            or head["entry_hash"] != previous.entry_hash
            or head["history_size"] != len(history_payload)
            or type(head["history_size"]) is not int
        ):
            _deny("authority_corrupt")
        if self._authority_floor_sequence >= 0:
            if (
                previous.sequence < self._authority_floor_sequence
                or len(entries) <= self._authority_floor_sequence
                or entries[self._authority_floor_sequence].entry_hash
                != self._authority_floor_hash
            ):
                _deny("authority_rollback")
        return previous

    def _open_state_domains(
        self,
        manifest: _AuthorityManifest,
    ) -> tuple[int, int, int]:
        parent_descriptor, parent_metadata = _open_parent_descriptor(
            self.state_directory,
            create=False,
        )
        anchor_descriptor = -1
        state_descriptor = -1
        try:
            anchor_descriptor, anchor_metadata = _open_directory_component(
                parent_descriptor,
                _NAMESPACE_DIRECTORY_NAME,
                create=False,
                private=True,
            )
            state_descriptor, state_metadata = _open_directory_component(
                parent_descriptor,
                self.state_directory.name,
                create=False,
                private=True,
            )
            namespace_identity = self._private_file_identity(
                anchor_descriptor,
                self._namespace_name,
                "state_directory_unsafe",
            )
            if (
                _directory_identity(parent_metadata) != manifest.state_parent_identity
                or _directory_identity(anchor_metadata) != manifest.anchor_identity
                or _directory_identity(state_metadata) != manifest.state_identity
                or namespace_identity != manifest.namespace_identity
            ):
                _deny("state_directory_unsafe")
            return parent_descriptor, anchor_descriptor, state_descriptor
        except OraclePolicyError:
            _close_descriptors(
                iter((state_descriptor, anchor_descriptor, parent_descriptor)),
                "state_directory_unsafe",
            )
            raise
        except (OSError, ValueError):
            _close_descriptors(
                iter((state_descriptor, anchor_descriptor, parent_descriptor)),
                "state_directory_unsafe",
            )
            _deny("state_directory_unsafe")

    def _read_namespace(
        self,
        anchor_descriptor: int,
        manifest: _AuthorityManifest,
        authority_entry: _AuthorityEntry,
    ) -> _NamespaceBinding:
        namespace_document = _read_private_json(
            anchor_descriptor,
            self._namespace_name,
            maximum_bytes=_MAX_NAMESPACE_BYTES,
            code="state_directory_unsafe",
            expected_identity=manifest.namespace_identity,
        )
        if namespace_document is None:
            _deny("state_directory_unsafe")
        decoded, payload = namespace_document
        if hashlib.sha256(payload).hexdigest() != authority_entry.namespace_sha256:
            _deny("authority_corrupt")
        binding = self._validate_namespace(
            decoded,
            authority_sequence=authority_entry.sequence,
            manifest=manifest,
        )
        if binding != authority_entry.binding:
            _deny("authority_corrupt")
        return binding

    def _assert_state_head(
        self,
        state_descriptor: int,
        binding: _NamespaceBinding,
    ) -> None:
        if binding.pending_head is not None:
            _deny("state_transition_incomplete")
        document = _read_private_json(
            state_descriptor,
            self.state_path.name,
            maximum_bytes=_MAX_STATE_BYTES,
            code="state_file_unsafe",
            content_code="state_corrupt",
        )
        if binding.state_head is None:
            if document is not None:
                _deny("state_file_unsafe")
            return
        if document is None:
            _deny("state_file_unsafe")
        decoded, payload = document
        if hashlib.sha256(payload).hexdigest() != binding.state_head.sha256:
            _deny("state_corrupt")
        if (
            not isinstance(decoded, Mapping)
            or type(decoded.get("revision")) is not int
            or decoded["revision"] != binding.state_head.revision
        ):
            _deny("state_corrupt")

    def _remember_authority_entry(self, entry: _AuthorityEntry) -> None:
        if entry.sequence > self._authority_floor_sequence:
            self._authority_floor_sequence = entry.sequence
            self._authority_floor_hash = entry.entry_hash

    def _attach_existing_authority(self) -> None:
        (
            authority_parent_descriptor,
            _authority_parent_metadata,
            authority_descriptor,
            _authority_metadata,
        ) = self._open_authority_domain()
        parent_descriptor = -1
        anchor_descriptor = -1
        state_descriptor = -1
        locked = False
        try:
            self._acquire_lock(authority_descriptor)
            locked = True
            manifest = self._load_manifest(
                authority_parent_descriptor,
                authority_descriptor,
            )
            authority_entry = self._load_authority_history(
                authority_descriptor,
                manifest,
            )
            if authority_entry.phase != "committed":
                _deny("state_transition_incomplete")
            (
                parent_descriptor,
                anchor_descriptor,
                state_descriptor,
            ) = self._open_state_domains(manifest)
            binding = self._read_namespace(
                anchor_descriptor,
                manifest,
                authority_entry,
            )
            self._assert_state_head(state_descriptor, binding)
            self._manifest = manifest
            self._remember_authority_entry(authority_entry)
        finally:
            try:
                if locked:
                    self._unlock(authority_descriptor)
            finally:
                _close_descriptors(
                    iter(
                        (
                            state_descriptor,
                            anchor_descriptor,
                            parent_descriptor,
                            authority_descriptor,
                            authority_parent_descriptor,
                        )
                    ),
                    "authority_directory_unsafe",
                )

    def _enroll_authority(self) -> None:
        authority_parent_descriptor = -1
        authority_descriptor = -1
        parent_descriptor = -1
        anchor_descriptor = -1
        state_descriptor = -1
        locked = False
        try:
            authority_parent_descriptor, authority_parent_metadata = (
                _open_parent_descriptor(
                    self.authority_directory,
                    create=True,
                    code="authority_directory_unsafe",
                )
            )
            parent_descriptor, parent_metadata = _open_parent_descriptor(
                self.state_directory,
                create=True,
            )
            authority_descriptor, authority_metadata = (
                _create_private_directory_component(
                    authority_parent_descriptor,
                    self.authority_directory.name,
                    code="authority_directory_unsafe",
                    exists_code="authority_already_enrolled",
                )
            )
            state_descriptor, state_metadata = _create_private_directory_component(
                parent_descriptor,
                self.state_directory.name,
                code="state_directory_unsafe",
                exists_code="authority_already_enrolled",
            )
            anchor_descriptor, anchor_metadata = _open_directory_component(
                parent_descriptor,
                _NAMESPACE_DIRECTORY_NAME,
                create=True,
                private=True,
                code="state_directory_unsafe",
            )
            self._acquire_lock(authority_descriptor)
            locked = True
            manifest_identity = _create_empty_private_file(
                authority_descriptor,
                _AUTHORITY_MANIFEST_NAME,
                code="authority_file_unsafe",
            )
            head_identity = _create_empty_private_file(
                authority_descriptor,
                _AUTHORITY_HEAD_NAME,
                code="authority_file_unsafe",
            )
            history_identity = _create_empty_private_file(
                authority_descriptor,
                _AUTHORITY_HISTORY_NAME,
                code="authority_file_unsafe",
            )
            namespace_identity = _create_empty_private_file(
                anchor_descriptor,
                self._namespace_name,
                code="state_directory_unsafe",
            )
            manifest = _AuthorityManifest(
                authority_generation=_secure_token_hex(
                    32,
                    "authority_generation_failed",
                ),
                namespace_generation=_secure_token_hex(
                    32,
                    "namespace_generation_failed",
                ),
                authority_parent_identity=_directory_identity(
                    authority_parent_metadata
                ),
                authority_identity=_directory_identity(authority_metadata),
                manifest_identity=manifest_identity,
                head_identity=head_identity,
                history_identity=history_identity,
                state_parent_identity=_directory_identity(parent_metadata),
                anchor_identity=_directory_identity(anchor_metadata),
                state_identity=_directory_identity(state_metadata),
                namespace_identity=namespace_identity,
            )
            manifest_payload = _canonical_json_bytes(
                self._manifest_document(manifest),
                "authority_corrupt",
            )
            _rewrite_private_file(
                authority_descriptor,
                _AUTHORITY_MANIFEST_NAME,
                manifest_payload,
                expected_identity=manifest.manifest_identity,
                maximum_bytes=_MAX_AUTHORITY_MANIFEST_BYTES,
                code="authority_file_unsafe",
            )
            binding = _NamespaceBinding(
                generation=manifest.namespace_generation,
                state_head=None,
                pending_head=None,
            )
            namespace_payload = self._namespace_payload(binding, 0, manifest)
            _rewrite_private_file(
                anchor_descriptor,
                self._namespace_name,
                namespace_payload,
                expected_identity=manifest.namespace_identity,
                maximum_bytes=_MAX_NAMESPACE_BYTES,
                code="state_directory_unsafe",
            )
            entry_payload = _canonical_json_bytes(
                self._authority_entry_document(
                    sequence=0,
                    previous_hash="0" * 64,
                    phase="committed",
                    binding=binding,
                    namespace_sha256=hashlib.sha256(namespace_payload).hexdigest(),
                    manifest=manifest,
                ),
                "authority_corrupt",
            )
            history_size = _append_private_file(
                authority_descriptor,
                _AUTHORITY_HISTORY_NAME,
                entry_payload,
                expected_identity=manifest.history_identity,
                expected_size=0,
                maximum_bytes=_MAX_AUTHORITY_HISTORY_BYTES,
                code="authority_file_unsafe",
            )
            entry = _AuthorityEntry(
                sequence=0,
                entry_hash=hashlib.sha256(entry_payload).hexdigest(),
                history_size=history_size,
                phase="committed",
                binding=binding,
                namespace_sha256=hashlib.sha256(namespace_payload).hexdigest(),
            )
            head_payload = _canonical_json_bytes(
                self._authority_head_document(entry, manifest),
                "authority_corrupt",
            )
            _rewrite_private_file(
                authority_descriptor,
                _AUTHORITY_HEAD_NAME,
                head_payload,
                expected_identity=manifest.head_identity,
                maximum_bytes=_MAX_AUTHORITY_HEAD_BYTES,
                code="authority_file_unsafe",
            )
        finally:
            try:
                if locked:
                    self._unlock(authority_descriptor)
            finally:
                _close_descriptors(
                    iter(
                        (
                            state_descriptor,
                            anchor_descriptor,
                            parent_descriptor,
                            authority_descriptor,
                            authority_parent_descriptor,
                        )
                    ),
                    "authority_directory_unsafe",
                )

    def _assert_lock_domain(
        self,
        locked: _LockedAuthority,
        *,
        metadata_may_change: bool = False,
    ) -> None:
        try:
            opened_authority_parent = os.fstat(locked.authority_parent_descriptor)
            opened_authority = os.fstat(locked.authority_descriptor)
            opened_parent = os.fstat(locked.parent_descriptor)
            opened_anchor = os.fstat(locked.anchor_descriptor)
            opened_state = os.fstat(locked.state_descriptor)
        except (OSError, ValueError):
            _deny("state_directory_unsafe")
        _verify_trusted_parent_metadata(
            opened_authority_parent,
            "authority_directory_unsafe",
        )
        _verify_private_directory_metadata(
            opened_authority,
            "authority_directory_unsafe",
        )
        _verify_trusted_parent_metadata(opened_parent)
        _verify_private_directory_metadata(opened_anchor)
        _verify_private_directory_metadata(opened_state)
        if (
            _directory_identity(opened_authority_parent)
            != locked.manifest.authority_parent_identity
            or _directory_identity(opened_authority)
            != locked.manifest.authority_identity
            or _directory_identity(opened_parent)
            != locked.manifest.state_parent_identity
            or _directory_identity(opened_anchor) != locked.manifest.anchor_identity
            or _directory_identity(opened_state) != locked.manifest.state_identity
            or (
                not metadata_may_change
                and opened_state.st_ctime_ns != locked.state_ctime_ns
            )
        ):
            _deny("state_directory_unsafe")
        manifest = self._load_manifest(
            locked.authority_parent_descriptor,
            locked.authority_descriptor,
        )
        if manifest != locked.manifest or manifest != self._manifest:
            _deny("authority_directory_unsafe")
        authority_entry = self._load_authority_history(
            locked.authority_descriptor,
            manifest,
        )
        if authority_entry != locked.authority_entry:
            _deny("authority_corrupt")
        binding = self._read_namespace(
            locked.anchor_descriptor,
            manifest,
            authority_entry,
        )
        if binding != locked.binding:
            _deny("authority_corrupt")
        if authority_entry.phase != "committed":
            _deny("state_transition_incomplete")
        self._assert_state_head(locked.state_descriptor, binding)
        (
            named_authority_parent,
            _named_authority_parent_metadata,
            named_authority,
            _named_authority_metadata,
        ) = self._open_authority_domain()
        named_parent = -1
        named_anchor = -1
        named_state = -1
        try:
            named_manifest = self._load_manifest(
                named_authority_parent,
                named_authority,
            )
            if named_manifest != manifest:
                _deny("authority_directory_unsafe")
            named_parent, named_anchor, named_state = self._open_state_domains(manifest)
        finally:
            _close_descriptors(
                iter(
                    (
                        named_state,
                        named_anchor,
                        named_parent,
                        named_authority,
                        named_authority_parent,
                    )
                ),
                "authority_directory_unsafe",
            )

    def _now(self) -> int:
        try:
            value = self._clock()
        except Exception:
            _deny("clock_invalid")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            _deny("clock_invalid")
        return int(value)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[_LockedAuthority]:
        (
            authority_parent_descriptor,
            _authority_parent_metadata,
            authority_descriptor,
            _authority_metadata,
        ) = self._open_authority_domain()
        parent_descriptor = -1
        anchor_descriptor = -1
        state_descriptor = -1
        locked = False
        try:
            self._acquire_lock(authority_descriptor)
            locked = True
            manifest = self._load_manifest(
                authority_parent_descriptor,
                authority_descriptor,
            )
            if manifest != self._manifest:
                _deny("authority_directory_unsafe")
            authority_entry = self._load_authority_history(
                authority_descriptor,
                manifest,
            )
            if authority_entry.phase != "committed":
                _deny("state_transition_incomplete")
            parent_descriptor, anchor_descriptor, state_descriptor = (
                self._open_state_domains(manifest)
            )
            binding = self._read_namespace(
                anchor_descriptor,
                manifest,
                authority_entry,
            )
            try:
                state_metadata = os.fstat(state_descriptor)
            except (OSError, ValueError):
                _deny("state_directory_unsafe")
            lock_domain = _LockedAuthority(
                authority_parent_descriptor=authority_parent_descriptor,
                authority_descriptor=authority_descriptor,
                parent_descriptor=parent_descriptor,
                anchor_descriptor=anchor_descriptor,
                state_descriptor=state_descriptor,
                manifest=manifest,
                authority_entry=authority_entry,
                binding=binding,
                state_ctime_ns=state_metadata.st_ctime_ns,
            )
            self._assert_lock_domain(lock_domain)
            yield lock_domain
            self._assert_lock_domain(lock_domain, metadata_may_change=True)
            self._remember_authority_entry(lock_domain.authority_entry)
        finally:
            try:
                if locked:
                    self._unlock(authority_descriptor)
            finally:
                _close_descriptors(
                    iter(
                        (
                            state_descriptor,
                            anchor_descriptor,
                            parent_descriptor,
                            authority_descriptor,
                            authority_parent_descriptor,
                        )
                    ),
                    "authority_directory_unsafe",
                )

    def _read_state(
        self,
        directory_descriptor: int,
        binding: _NamespaceBinding,
    ) -> dict[str, Any]:
        if binding.pending_head is not None:
            _deny("state_transition_incomplete")
        document = _read_private_json(
            directory_descriptor,
            self.state_path.name,
            maximum_bytes=_MAX_STATE_BYTES,
            code="state_file_unsafe",
            content_code="state_corrupt",
        )
        if document is None:
            if binding.state_head is not None:
                _deny("state_file_unsafe")
            return _initial_state(
                self.policy_fingerprint,
                binding.generation,
            )
        if binding.state_head is None:
            _deny("state_file_unsafe")
        decoded, payload = document
        if hashlib.sha256(payload).hexdigest() != binding.state_head.sha256:
            _deny("state_corrupt")
        return _validate_state(
            decoded,
            self.policy_fingerprint,
            binding.generation,
            binding.state_head.revision,
        )

    def _prepare_state_publication(
        self,
        state: dict[str, Any],
        binding: _NamespaceBinding,
    ) -> tuple[bytes, _StateHead]:
        if binding.pending_head is not None:
            _deny("state_transition_incomplete")
        committed_revision = (
            0 if binding.state_head is None else binding.state_head.revision
        )
        if committed_revision >= 2**63 - 1:
            _deny("state_corrupt")
        revision = committed_revision + 1
        state["revision"] = revision
        _validate_state(
            state,
            self.policy_fingerprint,
            binding.generation,
            revision,
        )
        payload = _canonical_json_bytes(state)
        if len(payload) > _MAX_STATE_BYTES:
            _deny("state_too_large")
        return payload, _StateHead(
            revision=revision,
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    def _write_state(
        self,
        payload: bytes,
        directory_descriptor: int,
        *,
        replace: bool,
    ) -> None:
        _atomic_write_private_file(
            directory_descriptor,
            self.state_path.name,
            payload,
            replace=replace,
            code="state_write_failed",
        )

    def _write_namespace(
        self,
        locked: _LockedAuthority,
        binding: _NamespaceBinding,
        authority_sequence: int,
    ) -> bytes:
        payload = self._namespace_payload(
            binding,
            authority_sequence,
            locked.manifest,
        )
        _rewrite_private_file(
            locked.anchor_descriptor,
            self._namespace_name,
            payload,
            expected_identity=locked.manifest.namespace_identity,
            maximum_bytes=_MAX_NAMESPACE_BYTES,
            code="state_directory_unsafe",
        )
        published = _read_private_json(
            locked.anchor_descriptor,
            self._namespace_name,
            maximum_bytes=_MAX_NAMESPACE_BYTES,
            code="state_directory_unsafe",
            expected_identity=locked.manifest.namespace_identity,
        )
        if published is None or published[1] != payload:
            _deny("state_directory_unsafe")
        if (
            self._validate_namespace(
                published[0],
                authority_sequence=authority_sequence,
                manifest=locked.manifest,
            )
            != binding
        ):
            _deny("state_directory_unsafe")
        return payload

    def _append_authority_transition(
        self,
        locked: _LockedAuthority,
        *,
        phase: str,
        binding: _NamespaceBinding,
        namespace_payload: bytes,
    ) -> None:
        sequence = locked.authority_entry.sequence + 1
        entry_payload = _canonical_json_bytes(
            self._authority_entry_document(
                sequence=sequence,
                previous_hash=locked.authority_entry.entry_hash,
                phase=phase,
                binding=binding,
                namespace_sha256=hashlib.sha256(namespace_payload).hexdigest(),
                manifest=locked.manifest,
            ),
            "authority_corrupt",
        )
        history_size = _append_private_file(
            locked.authority_descriptor,
            _AUTHORITY_HISTORY_NAME,
            entry_payload,
            expected_identity=locked.manifest.history_identity,
            expected_size=locked.authority_entry.history_size,
            maximum_bytes=_MAX_AUTHORITY_HISTORY_BYTES,
            code="authority_file_unsafe",
        )
        entry = _AuthorityEntry(
            sequence=sequence,
            entry_hash=hashlib.sha256(entry_payload).hexdigest(),
            history_size=history_size,
            phase=phase,
            binding=binding,
            namespace_sha256=hashlib.sha256(namespace_payload).hexdigest(),
        )
        self._validate_authority_transition(locked.authority_entry, entry)
        head_payload = _canonical_json_bytes(
            self._authority_head_document(entry, locked.manifest),
            "authority_corrupt",
        )
        _rewrite_private_file(
            locked.authority_descriptor,
            _AUTHORITY_HEAD_NAME,
            head_payload,
            expected_identity=locked.manifest.head_identity,
            maximum_bytes=_MAX_AUTHORITY_HEAD_BYTES,
            code="authority_file_unsafe",
        )
        locked.authority_entry = entry
        self._remember_authority_entry(entry)

    def _publish_pending_head(
        self,
        locked: _LockedAuthority,
        new_head: _StateHead,
    ) -> None:
        if locked.binding.pending_head is not None:
            _deny("state_transition_incomplete")
        pending = _NamespaceBinding(
            generation=locked.binding.generation,
            state_head=locked.binding.state_head,
            pending_head=new_head,
        )
        sequence = locked.authority_entry.sequence + 1
        namespace_payload = self._namespace_payload(
            pending,
            sequence,
            locked.manifest,
        )
        self._append_authority_transition(
            locked,
            phase="pending",
            binding=pending,
            namespace_payload=namespace_payload,
        )
        published = self._write_namespace(
            locked,
            pending,
            sequence,
        )
        if published != namespace_payload:
            _deny("state_directory_unsafe")
        locked.binding = pending

    def _commit_pending_head(self, locked: _LockedAuthority) -> None:
        pending_head = locked.binding.pending_head
        if pending_head is None:
            _deny("state_directory_unsafe")
        state_document = _read_private_json(
            locked.state_descriptor,
            self.state_path.name,
            maximum_bytes=_MAX_STATE_BYTES,
            code="state_file_unsafe",
            content_code="state_corrupt",
        )
        if state_document is None:
            _deny("state_file_unsafe")
        decoded, payload = state_document
        if (
            hashlib.sha256(payload).hexdigest() != pending_head.sha256
            or not isinstance(decoded, Mapping)
            or type(decoded.get("revision")) is not int
            or decoded["revision"] != pending_head.revision
        ):
            _deny("state_corrupt")
        committed = _NamespaceBinding(
            generation=locked.binding.generation,
            state_head=pending_head,
            pending_head=None,
        )
        sequence = locked.authority_entry.sequence + 1
        namespace_payload = self._write_namespace(
            locked,
            committed,
            sequence,
        )
        self._append_authority_transition(
            locked,
            phase="committed",
            binding=committed,
            namespace_payload=namespace_payload,
        )
        locked.binding = committed

    def _update(
        self,
        mutate: Callable[[dict[str, Any]], tuple[dict[str, Any], Any]],
    ) -> Any:
        with self._locked() as locked:
            self._assert_lock_domain(locked)
            state = self._read_state(
                locked.state_descriptor,
                locked.binding,
            )
            self._assert_lock_domain(locked)
            new_state, result = mutate(state)
            self._assert_lock_domain(locked)
            payload, new_head = self._prepare_state_publication(
                new_state,
                locked.binding,
            )
            replace_state = locked.binding.state_head is not None
            self._publish_pending_head(locked, new_head)
            self._write_state(
                payload,
                locked.state_descriptor,
                replace=replace_state,
            )
            self._commit_pending_head(locked)
            self._assert_lock_domain(locked, metadata_may_change=True)
            return result

    def _prepare_state(self, state: dict[str, Any], now: int) -> dict[str, Any]:
        if now < state["last_seen_at"]:
            _deny("clock_rollback")
        configured_callers = self.policy.callers
        state["callers"] = {
            caller_id: bucket
            for caller_id, bucket in state["callers"].items()
            if caller_id in configured_callers
        }
        for caller_id, bucket in state["callers"].items():
            limits = configured_callers[caller_id]
            cutoff = now - limits.window_seconds
            bucket["events"] = [
                event for event in bucket["events"] if event["at"] > cutoff
            ]
            bucket["reservations"] = {
                reservation_id: reservation
                for reservation_id, reservation in bucket["reservations"].items()
                if reservation["expires_at"] > now
            }
        state["last_seen_at"] = now
        return state

    @staticmethod
    def _check_request_limits(
        request: OracleRequestFacts,
        limits: CallerPolicy,
        request_bytes: int,
    ) -> None:
        if request.mode not in limits.modes:
            _deny("mode_denied")
        if request.prompt_bytes > limits.max_prompt_bytes:
            _deny("prompt_too_large")
        if request.file_count > limits.max_files:
            _deny("file_count_exceeded")
        if request.attachment_bytes > limits.max_attachment_bytes:
            _deny("attachment_bytes_exceeded")
        if request_bytes > limits.max_request_bytes:
            _deny("request_too_large")
        if request.timeout_seconds > limits.max_runtime_seconds:
            _deny("runtime_exceeded")

    def reserve(
        self,
        authenticated_caller: str,
        request: OracleRequestFacts,
    ) -> PolicyGrant:
        request, request_bytes = _validated_request_snapshot(request)
        if (
            not isinstance(authenticated_caller, str)
            or CALLER_ID_PATTERN.fullmatch(authenticated_caller) is None
        ):
            _deny("caller_denied")
        limits = self.policy.callers.get(authenticated_caller)
        if limits is None:
            _deny("caller_denied")
        self._check_request_limits(request, limits, request_bytes)
        now = self._now()

        def mutate(state: dict[str, Any]) -> tuple[dict[str, Any], PolicyGrant]:
            state = self._prepare_state(state, now)
            bucket = state["callers"].setdefault(
                authenticated_caller,
                {"events": [], "reservations": {}},
            )
            if len(bucket["reservations"]) >= limits.max_concurrent:
                _deny("concurrency_exceeded")
            if len(bucket["events"]) >= limits.max_requests_per_window:
                _deny("request_quota_exceeded")
            used_bytes = sum(event["bytes"] for event in bucket["events"])
            if used_bytes + request_bytes > limits.max_bytes_per_window:
                _deny("byte_quota_exceeded")
            for _attempt in range(8):
                reservation_id = _secure_token_hex(
                    16,
                    "reservation_id_failed",
                )
                if reservation_id not in bucket["reservations"]:
                    break
            else:
                _deny("reservation_id_failed")
            expires_at = now + request.timeout_seconds + limits.lease_grace_seconds
            bucket["events"].append({"at": now, "bytes": request_bytes})
            bucket["reservations"][reservation_id] = {
                "mode": request.mode,
                "admitted_at": now,
                "expires_at": expires_at,
                "request_bytes": request_bytes,
            }
            return state, PolicyGrant(
                caller_id=authenticated_caller,
                reservation_id=reservation_id,
                mode=request.mode,
                admitted_at=now,
                expires_at=expires_at,
                request_bytes=request_bytes,
            )

        return self._update(mutate)

    def release(self, authenticated_caller: str, reservation_id: str) -> bool:
        if (
            not isinstance(authenticated_caller, str)
            or CALLER_ID_PATTERN.fullmatch(authenticated_caller) is None
        ):
            _deny("caller_denied")
        if authenticated_caller not in self.policy.callers:
            _deny("caller_denied")
        if (
            not isinstance(reservation_id, str)
            or RESERVATION_ID_PATTERN.fullmatch(reservation_id) is None
        ):
            _deny("reservation_id_invalid")
        now = self._now()

        def mutate(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            state = self._prepare_state(state, now)
            bucket = state["callers"].get(authenticated_caller)
            if bucket is None:
                return state, False
            removed = bucket["reservations"].pop(reservation_id, None)
            return state, removed is not None

        return self._update(mutate)

    @contextlib.contextmanager
    def admission(
        self,
        authenticated_caller: str,
        request: OracleRequestFacts,
    ) -> Iterator[PolicyGrant]:
        """Reserve before yielding to any browser-facing code, then release."""

        grant = self.reserve(authenticated_caller, request)
        try:
            yield grant
        finally:
            self.release(grant.caller_id, grant.reservation_id)


def provision_oracle_policy_authority(
    policy: OraclePolicy,
    state_directory: str | Path,
    *,
    authority_directory: str | Path,
    lock_timeout_seconds: float = 2.0,
) -> None:
    """Enroll one local policy authority; never call from a remote request path."""

    OraclePolicyEngine._provision(
        policy,
        state_directory,
        authority_directory,
        lock_timeout_seconds,
    )
