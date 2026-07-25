"""Pinned, authenticated DCG (destructive command guard) distribution contract.

This module is the ONE repo-owned source of truth for which DCG build Skillbox
is allowed to install, and the only supported way to turn that pin into an
executable on disk. Nothing here follows ``latest``, curl-pipes a shell, or
trusts an asset that has not been both digest-matched AND minisign-verified
against the upstream release key.

Trust basis
-----------
* Version pin: ``v0.6.7`` from
  https://github.com/Dicklesworthstone/destructive_command_guard/releases/tag/v0.6.7
* Digests: the committed :data:`PINNED_ASSET_SHA256` table is a verbatim copy of
  the rows of that release's ``SHA256SUMS`` for the four supported assets. The
  full ``SHA256SUMS`` plus its ``.minisig`` are committed under
  ``tests/fixtures/dcg_distribution/`` so the pin can be re-proved offline.
* Signatures: every asset ships a detached ``<asset>.minisig`` signed by the
  upstream minisign key :data:`DCG_MINISIGN_PUBLIC_KEY` (key id
  :data:`DCG_MINISIGN_KEY_ID`). Both the asset signature and minisign's global
  signature over ``signature || trusted_comment`` are checked, and the trusted
  comment must name this exact version and asset, so a valid signature for a
  *different* asset or release cannot be replayed.

Fail-closed rules
-----------------
* An OS/architecture tuple that is not in :data:`PLATFORM_ASSETS` raises
  :class:`UnsupportedPlatformError`. Native Windows is deliberately absent; WSL
  is a Linux kernel and therefore takes the Linux mapping automatically.
* A digest mismatch, a missing/unparseable ``.minisig``, a wrong key id, or a
  trusted comment naming another asset/version raises. There is no advisory or
  "warn and continue" path, and no developer opt-out env var: an override that
  disagrees with the pin is rejected by :func:`validate_env_overrides`.
* With networking disabled and a cache miss, resolution raises
  :class:`OfflineCacheMissError` instead of silently skipping the install.

MCP drift
---------
DCG 0.6.7 renamed the stdio MCP bridge from ``dcg mcp`` to ``dcg mcp-server``.
:data:`DCG_MCP_COMMAND` is the current command; :data:`DCG_OBSOLETE_MCP_COMMAND`
is retained ONLY so callers and tests can assert the stale spelling is gone.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import lzma
import os
import platform
import subprocess
import tarfile
import urllib.error
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .distribution.http_security import HttpsOnlyError, require_https, secure_opener
from .errors import SkillboxError

__all__ = [
    "ARTIFACT_ID",
    "AssetPin",
    "ArchiveError",
    "DCG_MCP_COMMAND",
    "DCG_MINISIGN_KEY_ID",
    "DCG_MINISIGN_PUBLIC_KEY",
    "DCG_OBSOLETE_MCP_COMMAND",
    "DCG_RELEASE_DOWNLOAD_BASE",
    "DCG_RELEASE_SOURCE_COMMIT",
    "DCG_RELEASE_TAG_URL",
    "DCG_VERSION",
    "DcgDistributionError",
    "DigestMismatchError",
    "InstalledVersionError",
    "MetadataMissingError",
    "Minisig",
    "OfflineCacheMissError",
    "PINNED_ASSET_SHA256",
    "PLATFORM_ASSETS",
    "PinOverrideError",
    "Resolution",
    "SignatureError",
    "UnsupportedPlatformError",
    "asset_pin",
    "cache_dir",
    "cache_key",
    "default_cache_root",
    "extract_dcg_binary",
    "install_verified_binary",
    "installed_version",
    "mcp_command",
    "normalize_version",
    "parse_minisig",
    "probe_mcp_ready",
    "provenance_record",
    "resolve_asset",
    "resolve_verified_payload",
    "supported_assets",
    "supported_platforms",
    "sync_action",
    "validate_env_overrides",
    "verify_digest",
    "verify_minisign",
]


# --------------------------------------------------------------------------
# The pin. Changing anything in this block is a supply-chain change.
# --------------------------------------------------------------------------

ARTIFACT_ID = "dcg-bin"

DCG_VERSION = "v0.6.7"

DCG_REPO = "Dicklesworthstone/destructive_command_guard"

DCG_RELEASE_TAG_URL = f"https://github.com/{DCG_REPO}/releases/tag/{DCG_VERSION}"

DCG_RELEASE_DOWNLOAD_BASE = (
    f"https://github.com/{DCG_REPO}/releases/download/{DCG_VERSION}"
)

# Upstream minisign public key (minisign base64: "Ed" || 8-byte key id || key).
DCG_MINISIGN_PUBLIC_KEY = "RWTQoKUb0Ue4NsqTpPWnABCrIU0+m25zsMlbv6UcRClQ7jmRP3A7NmTB"

DCG_MINISIGN_KEY_ID = "d0a0a51bd147b836"

# The upstream trusted comments for v0.6.7 all end with this source commit.
DCG_RELEASE_SOURCE_COMMIT = "d847471364adf24d819c34a96058bc136cdc00b1"

# Verbatim rows from the v0.6.7 SHA256SUMS for the four supported assets.
PINNED_ASSET_SHA256: dict[str, str] = {
    "dcg-aarch64-apple-darwin.tar.xz":
        "dccfd90dbd77a75464784ae90be10e4356cf01856708ca8506ecb56da7e75e7f",
    "dcg-x86_64-apple-darwin.tar.xz":
        "4818359e58d21872160ed569884ed641935d5f74228bad30cd1faa4d43c11584",
    "dcg-aarch64-unknown-linux-gnu.tar.xz":
        "9d9edb541a03c0497e4472e5ca61747d476357ced077db452bb4811cee5cb77e",
    "dcg-x86_64-unknown-linux-musl.tar.xz":
        "6d90754b7170bdeb63375fd7d20e7dc330c56b8f1018fc45ccbbd5cccc1ca183",
}

# (os, machine) -> asset. Both normalized lowercase. Every unlisted tuple is
# rejected; native Windows (``windows``/``win32``) is intentionally absent even
# though upstream ships .zip builds, because Skillbox setup does not support it.
# WSL reports ``linux`` from ``platform.system()``, so it takes the Linux rows.
PLATFORM_ASSETS: dict[tuple[str, str], str] = {
    ("darwin", "arm64"): "dcg-aarch64-apple-darwin.tar.xz",
    ("darwin", "aarch64"): "dcg-aarch64-apple-darwin.tar.xz",
    ("darwin", "x86_64"): "dcg-x86_64-apple-darwin.tar.xz",
    ("darwin", "amd64"): "dcg-x86_64-apple-darwin.tar.xz",
    ("linux", "aarch64"): "dcg-aarch64-unknown-linux-gnu.tar.xz",
    ("linux", "arm64"): "dcg-aarch64-unknown-linux-gnu.tar.xz",
    ("linux", "x86_64"): "dcg-x86_64-unknown-linux-musl.tar.xz",
    ("linux", "amd64"): "dcg-x86_64-unknown-linux-musl.tar.xz",
}

# DCG 0.6.7 renamed ``dcg mcp`` -> ``dcg mcp-server``.
DCG_MCP_COMMAND = "mcp-server"
DCG_OBSOLETE_MCP_COMMAND = "mcp"

# Env knobs that predate the pin. They are retained as *assertions*, not
# opt-outs: a non-empty value that disagrees with the pin is a hard error.
PIN_URL_ENV = "SKILLBOX_DCG_DOWNLOAD_URL"
PIN_SHA256_ENV = "SKILLBOX_DCG_DOWNLOAD_SHA256"

BINARY_NAME = "dcg"

DEFAULT_TIMEOUT_SECONDS = 30.0

MCP_READY_TIMEOUT_SECONDS = 5.0

# Guard against a hostile/oversized response: the largest supported v0.6.7
# asset is ~6 MB, so 64 MB is generous while still bounded.
MAX_ASSET_BYTES = 64 * 1024 * 1024


# --------------------------------------------------------------------------
# Errors — every one carries a single remediation command.
# --------------------------------------------------------------------------


class DcgDistributionError(SkillboxError):
    """Base for every fail-closed DCG distribution error."""


class UnsupportedPlatformError(DcgDistributionError):
    """The running OS/architecture has no pinned, signed DCG asset."""


class DigestMismatchError(DcgDistributionError):
    """A downloaded or cached asset did not match the committed sha256."""


class SignatureError(DcgDistributionError):
    """An asset's minisign signature is missing, malformed, or wrong."""


class MetadataMissingError(DcgDistributionError):
    """Required release metadata (the detached .minisig) could not be read."""


class OfflineCacheMissError(DcgDistributionError):
    """Networking is disabled and the verified asset is not cached."""


class ArchiveError(DcgDistributionError):
    """The verified archive does not contain exactly one safe ``dcg`` member."""


class PinOverrideError(DcgDistributionError):
    """An env override disagrees with the repo-owned pin."""


class InstalledVersionError(DcgDistributionError):
    """The installed binary does not report the pinned version."""


def _remediate(profile: str = "core") -> str:
    return f"python3 .env-manager/manage.py sync --profile {profile}"


# --------------------------------------------------------------------------
# Platform / asset resolution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetPin:
    """One fully-resolved, verifiable download target."""

    asset: str
    sha256: str
    url: str
    minisig_url: str
    version: str
    minisign_key_id: str
    platform: str

    @property
    def cache_key(self) -> str:
        return cache_key(self.asset)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "sha256": self.sha256,
            "url": self.url,
            "minisig_url": self.minisig_url,
            "version": self.version,
            "minisign_key_id": self.minisign_key_id,
            "platform": self.platform,
            "cache_key": self.cache_key,
        }


def supported_assets() -> tuple[str, ...]:
    """Every asset name this repo is pinned to, sorted."""
    return tuple(sorted(PINNED_ASSET_SHA256))


def supported_platforms() -> tuple[str, ...]:
    """Every supported ``<os>/<machine>`` tuple, sorted, for remediation text."""
    return tuple(sorted(f"{system}/{machine}" for system, machine in PLATFORM_ASSETS))


def _normalize_platform(system: str | None, machine: str | None) -> tuple[str, str]:
    resolved_system = (system if system is not None else platform.system()).strip().lower()
    resolved_machine = (machine if machine is not None else platform.machine()).strip().lower()
    # ``uname -m`` spellings that mean the same silicon.
    if resolved_machine in {"x86-64", "x64", "amd64"}:
        resolved_machine = "x86_64"
    if resolved_machine in {"arm64", "armv8", "armv8l", "aarch64"}:
        resolved_machine = "aarch64"
    if resolved_system in {"win32", "cygwin", "msys", "windows"}:
        resolved_system = "windows"
    return resolved_system, resolved_machine


def resolve_asset(system: str | None = None, machine: str | None = None) -> str:
    """Return the pinned asset name for a platform, or fail closed.

    ``system``/``machine`` default to :func:`platform.system` /
    :func:`platform.machine`. WSL is not special-cased because its kernel
    reports ``Linux``; native Windows has no mapping and always raises.
    """
    resolved_system, resolved_machine = _normalize_platform(system, machine)
    asset = PLATFORM_ASSETS.get((resolved_system, resolved_machine))
    if asset is None:
        raise UnsupportedPlatformError(
            "DCG_UNSUPPORTED_PLATFORM",
            (
                f"no pinned DCG {DCG_VERSION} asset for "
                f"{resolved_system}/{resolved_machine}; supported platforms are "
                + ", ".join(supported_platforms())
            ),
            context={
                "system": resolved_system,
                "machine": resolved_machine,
                "version": DCG_VERSION,
                "supported_platforms": list(supported_platforms()),
            },
            next_actions=[
                "run Skillbox on a supported platform "
                "(macOS arm64/x86_64, Linux aarch64/x86_64, or WSL)"
            ],
            recoverable=False,
        )
    return asset


def asset_pin(system: str | None = None, machine: str | None = None) -> AssetPin:
    """Resolve the full :class:`AssetPin` for a platform."""
    asset = resolve_asset(system, machine)
    resolved_system, resolved_machine = _normalize_platform(system, machine)
    return AssetPin(
        asset=asset,
        sha256=PINNED_ASSET_SHA256[asset],
        url=f"{DCG_RELEASE_DOWNLOAD_BASE}/{asset}",
        minisig_url=f"{DCG_RELEASE_DOWNLOAD_BASE}/{asset}.minisig",
        version=DCG_VERSION,
        minisign_key_id=DCG_MINISIGN_KEY_ID,
        platform=f"{resolved_system}/{resolved_machine}",
    )


def cache_key(asset: str) -> str:
    """``dcg/<version>/<asset>/<sha256>`` — the contract's cache key."""
    sha256 = PINNED_ASSET_SHA256.get(asset)
    if sha256 is None:
        raise UnsupportedPlatformError(
            "DCG_UNSUPPORTED_PLATFORM",
            f"asset {asset!r} is not part of the pinned DCG {DCG_VERSION} set",
            context={"asset": asset, "supported_assets": list(supported_assets())},
            next_actions=[_remediate()],
            recoverable=False,
        )
    return f"{BINARY_NAME}/{DCG_VERSION}/{asset}/{sha256}"


def default_cache_root(env: Mapping[str, str] | None = None) -> Path:
    """Cache root that survives container replacement.

    ``$SKILLBOX_HOME_ROOT/.local`` is a persistent bind mount in
    ``docker-compose.yml``, so the verified asset cache lives under it (NOT
    under ``~/.cache``, which is not mounted and would be lost on replace).
    """
    values = env if env is not None else os.environ
    home_root = str(values.get("SKILLBOX_HOME_ROOT") or "").strip()
    base = Path(home_root) if home_root else Path.home()
    return base / ".local" / "share" / "skillbox" / "dcg"


def cache_dir(cache_root: Path | str, asset: str) -> Path:
    """Directory holding one cached asset + its detached signature."""
    return Path(cache_root) / cache_key(asset)


# --------------------------------------------------------------------------
# Verification primitives
# --------------------------------------------------------------------------


def verify_digest(payload: bytes, pin: AssetPin) -> str:
    """Return the sha256 of *payload*, raising unless it matches the pin."""
    actual = hashlib.sha256(payload).hexdigest()
    if actual != pin.sha256:
        raise DigestMismatchError(
            "DCG_DIGEST_MISMATCH",
            (
                f"DCG asset {pin.asset} digest mismatch: expected {pin.sha256}, "
                f"got {actual}"
            ),
            context={
                "asset": pin.asset,
                "url": pin.url,
                "expected_sha256": pin.sha256,
                "actual_sha256": actual,
                "version": pin.version,
            },
            next_actions=[_remediate()],
            recoverable=False,
        )
    return actual


@dataclass(frozen=True)
class Minisig:
    """A parsed minisign detached signature."""

    algorithm: str
    key_id: str
    signature: bytes
    trusted_comment: str
    global_signature: bytes


def _decode_minisign_public_key(config_str: str) -> tuple[str, Ed25519PublicKey]:
    try:
        raw = base64.b64decode(config_str.strip(), validate=True)
    except Exception as exc:  # noqa: BLE001 - normalized into a typed error
        raise SignatureError(
            "DCG_SIGNATURE_INVALID",
            f"minisign public key is not valid base64: {exc}",
            context={"public_key": config_str},
            next_actions=[_remediate()],
            recoverable=False,
        ) from exc
    if len(raw) != 42 or raw[:2] != b"Ed":
        raise SignatureError(
            "DCG_SIGNATURE_INVALID",
            "minisign public key must be 42 bytes beginning with 'Ed'",
            context={"public_key": config_str, "length": len(raw)},
            next_actions=[_remediate()],
            recoverable=False,
        )
    return raw[2:10].hex(), Ed25519PublicKey.from_public_bytes(raw[10:])


def parse_minisig(text: str) -> Minisig:
    """Parse a minisign ``.minisig`` file, failing closed on any malformation."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if len(lines) < 4:
        raise SignatureError(
            "DCG_SIGNATURE_INVALID",
            "minisign signature file must have an untrusted comment, a "
            "signature, a trusted comment, and a global signature",
            context={"line_count": len(lines)},
            next_actions=[_remediate()],
            recoverable=False,
        )
    trusted_marker = "trusted comment: "
    if not lines[2].startswith(trusted_marker):
        raise SignatureError(
            "DCG_SIGNATURE_INVALID",
            "minisign signature file is missing its trusted comment line",
            context={"line": lines[2][:120]},
            next_actions=[_remediate()],
            recoverable=False,
        )
    try:
        signature_blob = base64.b64decode(lines[1], validate=True)
        global_signature = base64.b64decode(lines[3], validate=True)
    except Exception as exc:  # noqa: BLE001 - normalized into a typed error
        raise SignatureError(
            "DCG_SIGNATURE_INVALID",
            f"minisign signature is not valid base64: {exc}",
            context={},
            next_actions=[_remediate()],
            recoverable=False,
        ) from exc
    if len(signature_blob) != 74:
        raise SignatureError(
            "DCG_SIGNATURE_INVALID",
            f"minisign signature must be 74 bytes, got {len(signature_blob)}",
            context={"length": len(signature_blob)},
            next_actions=[_remediate()],
            recoverable=False,
        )
    if len(global_signature) != 64:
        raise SignatureError(
            "DCG_SIGNATURE_INVALID",
            f"minisign global signature must be 64 bytes, got {len(global_signature)}",
            context={"length": len(global_signature)},
            next_actions=[_remediate()],
            recoverable=False,
        )
    algorithm = signature_blob[:2].decode("ascii", "replace")
    if algorithm not in {"Ed", "ED"}:
        raise SignatureError(
            "DCG_SIGNATURE_INVALID",
            f"unsupported minisign algorithm {algorithm!r}; expected 'Ed' or 'ED'",
            context={"algorithm": algorithm},
            next_actions=[_remediate()],
            recoverable=False,
        )
    return Minisig(
        algorithm=algorithm,
        key_id=signature_blob[2:10].hex(),
        signature=signature_blob[10:],
        trusted_comment=lines[2][len(trusted_marker):],
        global_signature=global_signature,
    )


def expected_trusted_comment(asset: str) -> str:
    """The exact trusted comment upstream stamps for *asset* at the pin."""
    return f"dcg {DCG_VERSION} {asset} source {DCG_RELEASE_SOURCE_COMMIT}"


def verify_minisign(
    payload: bytes,
    minisig_text: str,
    *,
    asset: str,
    public_key: str | None = None,
    require_trusted_comment: bool = True,
) -> Minisig:
    """Verify a detached minisign signature over *payload*.

    Checks, in order: parse, key id, payload signature (BLAKE2b-512 prehash for
    the ``ED`` algorithm), minisign's global signature over
    ``signature || trusted_comment``, and finally that the trusted comment names
    this exact version and asset so a signature from another asset or release
    cannot be replayed.
    """
    # Resolved at CALL time, not bind time, so the module-level pin stays the
    # single source of truth (and stays patchable in fixtures).
    key_id, key = _decode_minisign_public_key(
        public_key if public_key is not None else DCG_MINISIGN_PUBLIC_KEY
    )
    parsed = parse_minisig(minisig_text)
    if parsed.key_id != key_id:
        raise SignatureError(
            "DCG_SIGNATURE_INVALID",
            (
                f"DCG asset {asset} was signed by key id {parsed.key_id}, "
                f"expected the pinned upstream key {key_id}"
            ),
            context={
                "asset": asset,
                "signature_key_id": parsed.key_id,
                "expected_key_id": key_id,
            },
            next_actions=[_remediate()],
            recoverable=False,
        )
    message = (
        hashlib.blake2b(payload, digest_size=64).digest()
        if parsed.algorithm == "ED"
        else payload
    )
    try:
        key.verify(parsed.signature, message)
    except InvalidSignature as exc:
        raise SignatureError(
            "DCG_SIGNATURE_INVALID",
            f"DCG asset {asset} failed minisign verification against key {key_id}",
            context={"asset": asset, "expected_key_id": key_id},
            next_actions=[_remediate()],
            recoverable=False,
        ) from exc
    try:
        key.verify(
            parsed.global_signature,
            parsed.signature + parsed.trusted_comment.encode("utf-8"),
        )
    except InvalidSignature as exc:
        raise SignatureError(
            "DCG_SIGNATURE_INVALID",
            (
                f"DCG asset {asset} has a tampered trusted comment: the minisign "
                "global signature does not verify"
            ),
            context={"asset": asset, "trusted_comment": parsed.trusted_comment},
            next_actions=[_remediate()],
            recoverable=False,
        ) from exc
    if require_trusted_comment:
        expected = expected_trusted_comment(asset)
        if parsed.trusted_comment.strip() != expected:
            raise SignatureError(
                "DCG_SIGNATURE_INVALID",
                (
                    f"DCG asset {asset} trusted comment "
                    f"{parsed.trusted_comment.strip()!r} does not match the "
                    f"pinned {expected!r}"
                ),
                context={
                    "asset": asset,
                    "trusted_comment": parsed.trusted_comment.strip(),
                    "expected_trusted_comment": expected,
                },
                next_actions=[_remediate()],
                recoverable=False,
            )
    return parsed


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse a ``SHA256SUMS`` body into ``{filename: sha256}``."""
    rows: dict[str, str] = {}
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts[0].strip().lower(), parts[1].strip().lstrip("*")
        rows[name] = digest
    return rows


# --------------------------------------------------------------------------
# Archive extraction
# --------------------------------------------------------------------------


def extract_dcg_binary(payload: bytes, *, asset: str = "") -> bytes:
    """Return the ``dcg`` executable bytes from a verified ``.tar.xz`` payload.

    Only called AFTER digest + signature verification. Still refuses absolute
    paths, ``..`` traversal, symlinks, and any archive that does not contain
    exactly one regular file named ``dcg``.
    """
    try:
        decompressed = lzma.decompress(payload)
    except lzma.LZMAError as exc:
        raise ArchiveError(
            "DCG_ARCHIVE_INVALID",
            f"DCG asset {asset or '<unknown>'} is not a valid .tar.xz archive: {exc}",
            context={"asset": asset},
            next_actions=[_remediate()],
            recoverable=False,
        ) from exc
    try:
        archive = tarfile.open(fileobj=io.BytesIO(decompressed), mode="r:")
    except tarfile.TarError as exc:
        raise ArchiveError(
            "DCG_ARCHIVE_INVALID",
            f"DCG asset {asset or '<unknown>'} is not a valid tar archive: {exc}",
            context={"asset": asset},
            next_actions=[_remediate()],
            recoverable=False,
        ) from exc
    with archive:
        for member in archive.getmembers():
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ArchiveError(
                    "DCG_ARCHIVE_INVALID",
                    (
                        f"DCG asset {asset or '<unknown>'} contains an unsafe "
                        f"member path {member.name!r}"
                    ),
                    context={"asset": asset, "member": member.name},
                    next_actions=[_remediate()],
                    recoverable=False,
                )
        candidates = [
            member
            for member in archive.getmembers()
            if member.isfile() and PurePosixPath(member.name).name == BINARY_NAME
        ]
        if len(candidates) != 1:
            raise ArchiveError(
                "DCG_ARCHIVE_INVALID",
                (
                    f"DCG asset {asset or '<unknown>'} must contain exactly one "
                    f"regular file named {BINARY_NAME!r}; found {len(candidates)}"
                ),
                context={
                    "asset": asset,
                    "candidate_count": len(candidates),
                    "members": [member.name for member in archive.getmembers()][:32],
                },
                next_actions=[_remediate()],
                recoverable=False,
            )
        extracted = archive.extractfile(candidates[0])
        if extracted is None:
            raise ArchiveError(
                "DCG_ARCHIVE_INVALID",
                f"DCG asset {asset or '<unknown>'} member {candidates[0].name!r} is unreadable",
                context={"asset": asset, "member": candidates[0].name},
                next_actions=[_remediate()],
                recoverable=False,
            )
        return extracted.read()


# --------------------------------------------------------------------------
# Env override guard
# --------------------------------------------------------------------------


def validate_env_overrides(
    env: Mapping[str, str] | None = None,
    *,
    system: str | None = None,
    machine: str | None = None,
) -> AssetPin:
    """Reject any env "override" that disagrees with the repo-owned pin.

    ``SKILLBOX_DCG_DOWNLOAD_URL`` / ``SKILLBOX_DCG_DOWNLOAD_SHA256`` predate the
    pin. They are kept as assertions so an operator can *state* the expected
    values, but a non-empty value that disagrees is a hard failure — there is no
    developer opt-out that still counts as healthy.
    """
    values = env if env is not None else os.environ
    pin = asset_pin(system, machine)
    declared_url = str(values.get(PIN_URL_ENV) or "").strip()
    if declared_url and declared_url != pin.url:
        raise PinOverrideError(
            "DCG_PIN_OVERRIDE_REJECTED",
            (
                f"{PIN_URL_ENV}={declared_url} disagrees with the pinned DCG "
                f"{DCG_VERSION} asset URL {pin.url}"
            ),
            context={"env": PIN_URL_ENV, "declared": declared_url, "pinned": pin.url},
            next_actions=[f"unset {PIN_URL_ENV} or set it to {pin.url}"],
            recoverable=False,
        )
    declared_sha = str(values.get(PIN_SHA256_ENV) or "").strip().lower()
    if declared_sha and declared_sha != pin.sha256:
        raise PinOverrideError(
            "DCG_PIN_OVERRIDE_REJECTED",
            (
                f"{PIN_SHA256_ENV}={declared_sha} disagrees with the pinned DCG "
                f"{DCG_VERSION} digest {pin.sha256}"
            ),
            context={
                "env": PIN_SHA256_ENV,
                "declared": declared_sha,
                "pinned": pin.sha256,
            },
            next_actions=[f"unset {PIN_SHA256_ENV} or set it to {pin.sha256}"],
            recoverable=False,
        )
    return pin


# --------------------------------------------------------------------------
# Fetch + cache + resolve
# --------------------------------------------------------------------------

Fetcher = Callable[[str], bytes]


def _default_fetch(url: str) -> bytes:
    """HTTPS-only GET with a bounded read. Never follows a downgrade redirect."""
    require_https(url)
    opener = secure_opener()
    with opener.open(url, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
        payload = response.read(MAX_ASSET_BYTES + 1)
    if len(payload) > MAX_ASSET_BYTES:
        raise MetadataMissingError(
            "DCG_METADATA_UNREADABLE",
            f"DCG download from {url} exceeded the {MAX_ASSET_BYTES} byte bound",
            context={"url": url, "max_bytes": MAX_ASSET_BYTES},
            next_actions=[_remediate()],
            recoverable=False,
        )
    return payload


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving the pinned asset to verified bytes."""

    pin: AssetPin
    payload: bytes
    minisig_text: str
    source: str  # "cache" | "download"
    verified: bool = True

    @property
    def cache_key(self) -> str:
        return self.pin.cache_key


def _read_cached(directory: Path, asset: str) -> tuple[bytes, str] | None:
    asset_file = directory / asset
    minisig_file = directory / f"{asset}.minisig"
    if not asset_file.is_file() or not minisig_file.is_file():
        return None
    return asset_file.read_bytes(), minisig_file.read_text(encoding="utf-8")


def _write_cached(directory: Path, asset: str, payload: bytes, minisig_text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / asset).write_bytes(payload)
    (directory / f"{asset}.minisig").write_text(minisig_text, encoding="utf-8")


def resolve_verified_payload(
    *,
    cache_root: Path | str,
    system: str | None = None,
    machine: str | None = None,
    fetch: Fetcher | None = None,
    allow_network: bool = True,
    env: Mapping[str, str] | None = None,
) -> Resolution:
    """Resolve the pinned asset to digest- and signature-verified bytes.

    Cache hit and cache miss take the SAME verification path, so corrupting one
    byte of a cached asset fails exactly like a corrupted download. With
    ``allow_network=False`` and a cache miss this raises rather than skipping.
    """
    pin = validate_env_overrides(env, system=system, machine=machine)
    directory = cache_dir(cache_root, pin.asset)

    cached = _read_cached(directory, pin.asset)
    if cached is not None:
        payload, minisig_text = cached
        verify_digest(payload, pin)
        verify_minisign(payload, minisig_text, asset=pin.asset)
        return Resolution(pin=pin, payload=payload, minisig_text=minisig_text, source="cache")

    if not allow_network:
        raise OfflineCacheMissError(
            "DCG_OFFLINE_CACHE_MISS",
            (
                f"DCG {DCG_VERSION} asset {pin.asset} is not in the verified cache "
                f"at {directory} and networking is disabled"
            ),
            context={
                "asset": pin.asset,
                "version": pin.version,
                "cache_key": pin.cache_key,
                "cache_dir": str(directory),
            },
            next_actions=[
                f"restore the cache at {directory} or rerun with network access"
            ],
            recoverable=True,
        )

    fetcher = fetch or _default_fetch
    payload = _fetch_or_raise(fetcher, pin.url, pin.asset, kind="asset")
    minisig_bytes = _fetch_or_raise(fetcher, pin.minisig_url, pin.asset, kind="signature")
    minisig_text = minisig_bytes.decode("utf-8", "replace")

    verify_digest(payload, pin)
    verify_minisign(payload, minisig_text, asset=pin.asset)
    _write_cached(directory, pin.asset, payload, minisig_text)
    return Resolution(pin=pin, payload=payload, minisig_text=minisig_text, source="download")


def _fetch_or_raise(fetch: Fetcher, url: str, asset: str, *, kind: str) -> bytes:
    try:
        return fetch(url)
    except DcgDistributionError:
        raise
    except (HttpsOnlyError, urllib.error.URLError, OSError, ValueError) as exc:
        raise MetadataMissingError(
            "DCG_METADATA_UNREADABLE",
            f"could not fetch DCG {DCG_VERSION} {kind} for {asset} from {url}: {exc}",
            context={"asset": asset, "url": url, "kind": kind},
            next_actions=[_remediate()],
            recoverable=True,
        ) from exc


# --------------------------------------------------------------------------
# Install + provenance
# --------------------------------------------------------------------------


def installed_version(
    binary_path: Path | str,
    *,
    runner: Callable[..., Any] | None = None,
) -> str:
    """Return the normalized ``vX.Y.Z`` reported by ``<binary> --version``."""
    path = Path(binary_path)
    if not path.is_file():
        raise InstalledVersionError(
            "DCG_BINARY_MISSING",
            f"DCG binary is not installed at {path}",
            context={"path": str(path), "expected_version": DCG_VERSION},
            next_actions=[_remediate()],
            recoverable=True,
        )
    run = runner or subprocess.run
    try:
        completed = run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstalledVersionError(
            "DCG_VERSION_UNREADABLE",
            f"could not run {path} --version: {exc}",
            context={"path": str(path)},
            next_actions=[_remediate()],
            recoverable=True,
        ) from exc
    if getattr(completed, "returncode", 1) != 0:
        raise InstalledVersionError(
            "DCG_VERSION_UNREADABLE",
            f"{path} --version exited {getattr(completed, 'returncode', 'unknown')}",
            context={"path": str(path), "returncode": getattr(completed, "returncode", None)},
            next_actions=[_remediate()],
            recoverable=True,
        )
    return normalize_version(f"{completed.stdout or ''}\n{completed.stderr or ''}")


def normalize_version(text: str) -> str:
    """Pull the first ``X.Y.Z`` out of ``dcg --version`` output as ``vX.Y.Z``.

    DCG 0.6.7 prints a bare ``0.6.7`` followed by a banner, so this scans tokens
    rather than assuming a single-line, single-token response.
    """
    for raw_token in (text or "").replace("\r", " ").replace("\n", " ").split():
        token = raw_token.strip().strip(",;()[]").lstrip("vV")
        parts = token.split(".")
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            return "v" + ".".join(str(int(part)) for part in parts)
    raise InstalledVersionError(
        "DCG_VERSION_UNREADABLE",
        f"could not parse a semantic version out of {text.strip()[:120]!r}",
        context={"output": (text or "").strip()[:400]},
        next_actions=[_remediate()],
        recoverable=True,
    )


def install_verified_binary(
    target_path: Path | str,
    *,
    cache_root: Path | str,
    system: str | None = None,
    machine: str | None = None,
    fetch: Fetcher | None = None,
    allow_network: bool = True,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Converge ``target_path`` to the pinned, verified DCG binary.

    Returns the structured provenance/action record. Idempotent: an already
    correct install returns ``state="ok"`` / ``action="exists"`` and touches
    nothing. A binary reporting another version is ``state="stale"`` and is
    replaced.
    """
    path = Path(target_path)
    pin = validate_env_overrides(env, system=system, machine=machine)

    state = "missing"
    current_version = ""
    if path.is_file():
        try:
            current_version = installed_version(path)
        except InstalledVersionError:
            current_version = ""
        state = "ok" if current_version == pin.version else "stale"

    if state == "ok":
        return sync_action(
            pin,
            action="exists",
            state=state,
            path=path,
            source="installed",
            installed_version_value=current_version,
        )

    if dry_run:
        return sync_action(
            pin,
            action="install" if state == "missing" else "reinstall",
            state=state,
            path=path,
            source="planned",
            installed_version_value=current_version,
            dry_run=True,
        )

    resolution = resolve_verified_payload(
        cache_root=cache_root,
        system=system,
        machine=machine,
        fetch=fetch,
        allow_network=allow_network,
        env=env,
    )
    binary_bytes = extract_dcg_binary(resolution.payload, asset=pin.asset)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.dcg-install")
    temp_path.write_bytes(binary_bytes)
    temp_path.chmod(0o755)
    temp_path.replace(path)

    return sync_action(
        pin,
        action="install" if state == "missing" else "reinstall",
        state=state,
        path=path,
        source=resolution.source,
        installed_version_value=pin.version,
    )


def sync_action(
    pin: AssetPin,
    *,
    action: str,
    state: str,
    path: Path | str,
    source: str,
    installed_version_value: str = "",
    dry_run: bool = False,
    verified: bool = True,
) -> dict[str, Any]:
    """The structured, observable provenance record for one converge step.

    Shape is stable and consumed by the lifecycle/doctor surfaces:
    ``{"id": "dcg-bin", "version": "v0.6.7", "verified": true, ...}``.
    """
    return {
        "id": ARTIFACT_ID,
        "action": action,
        "state": state,
        "version": pin.version,
        "verified": bool(verified),
        "asset": pin.asset,
        "sha256": pin.sha256,
        "minisign_key_id": pin.minisign_key_id,
        "cache_key": pin.cache_key,
        "platform": pin.platform,
        "url": pin.url,
        "path": str(path),
        "source": source,
        "installed_version": installed_version_value,
        "mcp_command": DCG_MCP_COMMAND,
        "dry_run": bool(dry_run),
    }


def provenance_record(
    binary_path: Path | str,
    *,
    system: str | None = None,
    machine: str | None = None,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Observable provenance for an already-installed binary.

    Raises :class:`InstalledVersionError` unless the installed binary reports
    exactly the pinned version — a present-but-wrong binary is never healthy.
    """
    pin = asset_pin(system, machine)
    reported = installed_version(binary_path, runner=runner)
    if reported != pin.version:
        raise InstalledVersionError(
            "DCG_VERSION_MISMATCH",
            (
                f"installed DCG at {binary_path} reports {reported}, "
                f"expected the pinned {pin.version}"
            ),
            context={
                "path": str(binary_path),
                "installed_version": reported,
                "expected_version": pin.version,
            },
            next_actions=[_remediate()],
            recoverable=True,
        )
    record = sync_action(
        pin,
        action="verify",
        state="ok",
        path=binary_path,
        source="installed",
        installed_version_value=reported,
    )
    return record


# --------------------------------------------------------------------------
# MCP readiness (the 0.6.7 ``mcp`` -> ``mcp-server`` drift)
# --------------------------------------------------------------------------


def mcp_command(binary_path: Path | str) -> list[str]:
    """The current stdio MCP bridge invocation: ``<dcg> mcp-server``."""
    return [str(binary_path), DCG_MCP_COMMAND]


def probe_mcp_ready(
    binary_path: Path | str,
    *,
    timeout: float = MCP_READY_TIMEOUT_SECONDS,
    subcommand: str = DCG_MCP_COMMAND,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run a bounded stdio ``initialize`` handshake against the MCP bridge.

    Returns ``{"ready": bool, ...}``. ``ready`` is True only when the process
    answers a JSON-RPC ``initialize`` with a matching id and a ``result``, which
    is why ``dcg mcp`` (removed in 0.6.7) fails this contract while
    ``dcg mcp-server`` passes.
    """
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "skillbox-dcg-readiness", "version": "1"},
        },
    }
    run = runner or subprocess.run
    argv = [str(binary_path), subcommand]
    try:
        completed = run(
            argv,
            input=json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ready": False,
            "command": subcommand,
            "argv": argv,
            "reason": f"no initialize response within {timeout}s",
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ready": False, "command": subcommand, "argv": argv, "reason": str(exc)}

    stdout = getattr(completed, "stdout", "") or ""
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            message = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or message.get("id") != 1:
            continue
        if "result" not in message:
            continue
        server_info = (message.get("result") or {}).get("serverInfo") or {}
        return {
            "ready": True,
            "command": subcommand,
            "argv": argv,
            "server_name": server_info.get("name", ""),
            "server_version": server_info.get("version", ""),
            "protocol_version": (message.get("result") or {}).get("protocolVersion", ""),
        }
    return {
        "ready": False,
        "command": subcommand,
        "argv": argv,
        "reason": (
            (getattr(completed, "stderr", "") or stdout or "no output").strip()[:200]
        ),
    }


def mcp_readiness_report(
    binary_path: Path | str,
    *,
    timeout: float = MCP_READY_TIMEOUT_SECONDS,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Assert BOTH halves of the MCP contract at once.

    ``mcp-server`` must be ready and the obsolete ``mcp`` spelling must not be.
    """
    current = probe_mcp_ready(binary_path, timeout=timeout, runner=runner)
    obsolete = probe_mcp_ready(
        binary_path,
        timeout=timeout,
        subcommand=DCG_OBSOLETE_MCP_COMMAND,
        runner=runner,
    )
    return {
        "command": DCG_MCP_COMMAND,
        "obsolete_command": DCG_OBSOLETE_MCP_COMMAND,
        "ready": bool(current.get("ready")) and not obsolete.get("ready"),
        "current": current,
        "obsolete": obsolete,
    }


def describe_pin(system: str | None = None, machine: str | None = None) -> dict[str, Any]:
    """Machine-readable summary of the pin for this platform."""
    pin = asset_pin(system, machine)
    payload = pin.to_dict()
    payload.update(
        {
            "id": ARTIFACT_ID,
            "release_tag_url": DCG_RELEASE_TAG_URL,
            "release_source_commit": DCG_RELEASE_SOURCE_COMMIT,
            "minisign_public_key": DCG_MINISIGN_PUBLIC_KEY,
            "mcp_command": DCG_MCP_COMMAND,
            "supported_assets": list(supported_assets()),
            "supported_platforms": list(supported_platforms()),
        }
    )
    return payload


def _cli(argv: Sequence[str] | None = None) -> int:
    """``python3 -m runtime_manager.dcg_distribution [--platform os/machine]``."""
    import argparse

    parser = argparse.ArgumentParser(prog="dcg_distribution")
    parser.add_argument("--platform", default="", help="os/machine override")
    parser.add_argument("--binary", default="", help="verify an installed binary")
    args = parser.parse_args(list(argv) if argv is not None else None)

    system = machine = None
    if args.platform:
        system, _, machine = args.platform.partition("/")

    try:
        if args.binary:
            payload = provenance_record(args.binary, system=system, machine=machine)
        else:
            payload = describe_pin(system, machine)
    except DcgDistributionError as exc:
        print(json.dumps(exc.to_payload(), indent=2, sort_keys=True))
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    raise SystemExit(_cli())
