"""Tests for ed25519 signing primitives (WG-002)."""
from __future__ import annotations

import base64
import copy
import json
import os
import sys
import unittest

# Ensure the .env-manager package is importable.
ENV_MANAGER_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, ".env-manager")
sys.path.insert(0, os.path.abspath(ENV_MANAGER_DIR))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runtime_manager.distribution.signing import (
    ALGORITHM,
    KEY_PREFIX,
    KeyFormatError,
    SignatureVerificationError,
    _canonicalize,
    load_public_key,
    public_key_to_config_str,
    sign_detached,
    sign_manifest,
    verify_detached_signature,
    verify_manifest_signature,
)


def _make_keypair():
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    return private, public


def _config_str(public_key):
    return public_key_to_config_str(public_key)


SAMPLE_MANIFEST = {
    "schema_version": 1,
    "distributor_id": "acme-skills",
    "client_id": "client-42",
    "manifest_version": 14,
    "updated_at": "2026-04-21T10:00:00Z",
    "skills": [
        {
            "name": "deploy",
            "version": 8,
            "min_version": 7,
            "sha256": "abc123",
            "size_bytes": 28400,
            "download_url": "/skills/deploy/8/bundle.tar.gz",
            "targets": ["box"],
            "changelog": "Added rollback safety checks",
        }
    ],
}


class TestLoadPublicKey(unittest.TestCase):
    def test_valid_key(self):
        _, pub = _make_keypair()
        config = _config_str(pub)
        loaded = load_public_key(config)
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        self.assertEqual(
            loaded.public_bytes(Encoding.Raw, PublicFormat.Raw),
            pub.public_bytes(Encoding.Raw, PublicFormat.Raw),
        )

    def test_missing_prefix(self):
        with self.assertRaises(KeyFormatError):
            load_public_key("rsa:AAAA")

    def test_empty_string(self):
        with self.assertRaises(KeyFormatError):
            load_public_key("")

    def test_not_a_string(self):
        with self.assertRaises(KeyFormatError):
            load_public_key(12345)  # type: ignore[arg-type]

    def test_invalid_base64(self):
        with self.assertRaises(KeyFormatError):
            load_public_key("ed25519:not-valid-base64!!!")

    def test_wrong_key_length(self):
        short = base64.b64encode(b"\x00" * 16).decode()
        with self.assertRaises(KeyFormatError):
            load_public_key(f"ed25519:{short}")

    def test_wrong_algorithm_prefix(self):
        with self.assertRaises(KeyFormatError):
            load_public_key("rsa256:AAAA")


class TestManifestSignAndVerify(unittest.TestCase):
    def test_sign_and_verify_round_trip(self):
        priv, pub = _make_keypair()
        signed = sign_manifest(SAMPLE_MANIFEST, priv)
        self.assertIn("signature", signed)
        self.assertTrue(signed["signature"].startswith(KEY_PREFIX))
        verify_manifest_signature(signed, pub)

    def test_invalid_signature_raises(self):
        priv, pub = _make_keypair()
        signed = sign_manifest(SAMPLE_MANIFEST, priv)
        signed["signature"] = KEY_PREFIX + base64.b64encode(b"\x00" * 64).decode()
        with self.assertRaises(SignatureVerificationError):
            verify_manifest_signature(signed, pub)

    def test_wrong_key_rejects(self):
        priv1, _ = _make_keypair()
        _, pub2 = _make_keypair()
        signed = sign_manifest(SAMPLE_MANIFEST, priv1)
        with self.assertRaises(SignatureVerificationError):
            verify_manifest_signature(signed, pub2)

    def test_missing_signature_field(self):
        _, pub = _make_keypair()
        with self.assertRaises(SignatureVerificationError):
            verify_manifest_signature({"foo": "bar"}, pub)

    def test_malformed_signature_base64(self):
        _, pub = _make_keypair()
        manifest = dict(SAMPLE_MANIFEST)
        manifest["signature"] = "ed25519:not-valid!!!"
        with self.assertRaises(SignatureVerificationError):
            verify_manifest_signature(manifest, pub)

    def test_field_ordering_does_not_matter(self):
        """Verification succeeds regardless of dict iteration order."""
        priv, pub = _make_keypair()
        signed = sign_manifest(SAMPLE_MANIFEST, priv)
        reordered = dict(reversed(list(signed.items())))
        verify_manifest_signature(reordered, pub)

    def test_extra_fields_break_signature(self):
        """Adding a field after signing invalidates the signature."""
        priv, pub = _make_keypair()
        signed = sign_manifest(SAMPLE_MANIFEST, priv)
        signed["extra_field"] = "injected"
        with self.assertRaises(SignatureVerificationError):
            verify_manifest_signature(signed, pub)

    def test_sign_does_not_mutate_input(self):
        priv, _ = _make_keypair()
        original = copy.deepcopy(SAMPLE_MANIFEST)
        sign_manifest(SAMPLE_MANIFEST, priv)
        self.assertEqual(SAMPLE_MANIFEST, original)

    def test_sign_strips_existing_signature(self):
        """If the input already has a signature field, it is replaced."""
        priv, pub = _make_keypair()
        with_old_sig = dict(SAMPLE_MANIFEST, signature="ed25519:old")
        signed = sign_manifest(with_old_sig, priv)
        verify_manifest_signature(signed, pub)


class TestDetachedSignature(unittest.TestCase):
    def test_round_trip(self):
        priv, pub = _make_keypair()
        payload = b"some bundle manifest bytes"
        sig_json = sign_detached(payload, priv)
        self.assertEqual(sig_json["algorithm"], ALGORITHM)
        verify_detached_signature(payload, sig_json, pub)

    def test_invalid_signature_raises(self):
        priv, pub = _make_keypair()
        payload = b"data"
        sig_json = sign_detached(payload, priv)
        sig_json["signature"] = base64.b64encode(b"\x00" * 64).decode()
        with self.assertRaises(SignatureVerificationError):
            verify_detached_signature(payload, sig_json, pub)

    def test_wrong_algorithm_raises(self):
        priv, pub = _make_keypair()
        payload = b"data"
        sig_json = sign_detached(payload, priv)
        sig_json["algorithm"] = "rsa256"
        with self.assertRaises(SignatureVerificationError):
            verify_detached_signature(payload, sig_json, pub)

    def test_missing_signature_field(self):
        _, pub = _make_keypair()
        with self.assertRaises(SignatureVerificationError):
            verify_detached_signature(b"data", {"algorithm": "ed25519"}, pub)

    def test_wrong_key_rejects(self):
        priv1, _ = _make_keypair()
        _, pub2 = _make_keypair()
        payload = b"data"
        sig_json = sign_detached(payload, priv1)
        with self.assertRaises(SignatureVerificationError):
            verify_detached_signature(payload, sig_json, pub2)

    def test_tampered_payload_rejects(self):
        priv, pub = _make_keypair()
        payload = b"original"
        sig_json = sign_detached(payload, priv)
        with self.assertRaises(SignatureVerificationError):
            verify_detached_signature(b"tampered", sig_json, pub)

    def test_malformed_base64(self):
        _, pub = _make_keypair()
        with self.assertRaises(SignatureVerificationError):
            verify_detached_signature(
                b"data",
                {"algorithm": "ed25519", "signature": "!!!invalid!!!"},
                pub,
            )


class TestCanonicalize(unittest.TestCase):
    def test_sorted_keys(self):
        result = json.loads(_canonicalize({"z": 1, "a": 2}))
        self.assertEqual(list(result.keys()), ["a", "z"])

    def test_no_whitespace(self):
        raw = _canonicalize({"key": "value"})
        self.assertNotIn(b" ", raw)
        self.assertNotIn(b"\n", raw)

    def test_deterministic(self):
        d = {"b": [1, 2], "a": {"nested": True}}
        self.assertEqual(_canonicalize(d), _canonicalize(d))


class TestPublicKeyRoundTrip(unittest.TestCase):
    def test_config_str_round_trip(self):
        _, pub = _make_keypair()
        config = public_key_to_config_str(pub)
        self.assertTrue(config.startswith(KEY_PREFIX))
        loaded = load_public_key(config)
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        self.assertEqual(
            loaded.public_bytes(Encoding.Raw, PublicFormat.Raw),
            pub.public_bytes(Encoding.Raw, PublicFormat.Raw),
        )


if __name__ == "__main__":
    unittest.main()
