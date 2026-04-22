"""ed25519 signing primitives for skillbox distribution manifests and bundles."""
from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

KEY_PREFIX = "ed25519:"
ALGORITHM = "ed25519"


class SignatureVerificationError(Exception):
    pass


class KeyFormatError(Exception):
    pass


def load_public_key(config_str: str) -> Ed25519PublicKey:
    """Parse ``ed25519:<base64-encoded-32-byte-key>`` into an Ed25519PublicKey."""
    if not isinstance(config_str, str) or not config_str.startswith(KEY_PREFIX):
        raise KeyFormatError(
            f"expected key format '{KEY_PREFIX}<base64>', got: {config_str!r}"
        )
    b64 = config_str[len(KEY_PREFIX) :]
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception as exc:
        raise KeyFormatError(f"invalid base64 in public key: {exc}") from exc
    if len(raw) != 32:
        raise KeyFormatError(
            f"ed25519 public key must be 32 bytes, got {len(raw)}"
        )
    return Ed25519PublicKey.from_public_bytes(raw)


def public_key_to_config_str(key: Ed25519PublicKey) -> str:
    """Serialize an Ed25519PublicKey back to ``ed25519:<base64>`` config format."""
    raw = key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return KEY_PREFIX + base64.b64encode(raw).decode("ascii")


def _canonicalize(data: dict[str, Any]) -> bytes:
    """Canonical JSON: sorted keys, no whitespace, UTF-8."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_manifest(manifest: dict[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    """Add an ``ed25519:`` signature field to *manifest* (returns a new dict).

    Used by tests and Phase-2 distributor tooling; clients only verify.
    """
    signable = {k: v for k, v in manifest.items() if k != "signature"}
    payload = _canonicalize(signable)
    sig_bytes = private_key.sign(payload)
    signed = dict(manifest)
    signed["signature"] = KEY_PREFIX + base64.b64encode(sig_bytes).decode("ascii")
    return signed


def verify_manifest_signature(
    manifest: dict[str, Any],
    public_key: Ed25519PublicKey,
) -> None:
    """Verify the ``signature`` field of a manifest dict.

    Raises ``SignatureVerificationError`` if verification fails or the field
    is missing/malformed.
    """
    sig_value = manifest.get("signature")
    if not isinstance(sig_value, str) or not sig_value.startswith(KEY_PREFIX):
        raise SignatureVerificationError(
            "manifest missing or malformed 'signature' field"
        )
    try:
        sig_bytes = base64.b64decode(sig_value[len(KEY_PREFIX) :], validate=True)
    except Exception as exc:
        raise SignatureVerificationError(f"invalid base64 in signature: {exc}") from exc

    signable = {k: v for k, v in manifest.items() if k != "signature"}
    payload = _canonicalize(signable)
    try:
        public_key.verify(sig_bytes, payload)
    except InvalidSignature as exc:
        raise SignatureVerificationError("ed25519 signature verification failed") from exc


def sign_detached(payload: bytes, private_key: Ed25519PrivateKey) -> dict[str, str]:
    """Produce a detached signature JSON dict for arbitrary *payload* bytes."""
    sig_bytes = private_key.sign(payload)
    return {
        "algorithm": ALGORITHM,
        "signature": base64.b64encode(sig_bytes).decode("ascii"),
    }


def verify_detached_signature(
    payload: bytes,
    signature_json: dict[str, Any],
    public_key: Ed25519PublicKey,
) -> None:
    """Verify a detached ``{"algorithm": "ed25519", "signature": "<base64>"}`` object.

    Raises ``SignatureVerificationError`` on failure.
    """
    algo = signature_json.get("algorithm")
    if algo != ALGORITHM:
        raise SignatureVerificationError(
            f"unsupported signature algorithm: {algo!r}"
        )
    sig_b64 = signature_json.get("signature")
    if not isinstance(sig_b64, str):
        raise SignatureVerificationError("missing 'signature' field in detached signature")
    try:
        sig_bytes = base64.b64decode(sig_b64, validate=True)
    except Exception as exc:
        raise SignatureVerificationError(f"invalid base64 in detached signature: {exc}") from exc

    try:
        public_key.verify(sig_bytes, payload)
    except InvalidSignature as exc:
        raise SignatureVerificationError("ed25519 detached signature verification failed") from exc
