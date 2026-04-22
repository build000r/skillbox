"""Tests for per-client manifest schema (WG-005)."""
from __future__ import annotations

import json
import os
import sys
import unittest

ENV_MANAGER_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, ".env-manager")
sys.path.insert(0, os.path.abspath(ENV_MANAGER_DIR))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runtime_manager.distribution.manifest import (
    SUPPORTED_SCHEMA_VERSION,
    ClientManifest,
    ClientManifestSkill,
    ManifestSchemaError,
    filter_skills_for_target,
    parse_manifest,
    verify_manifest,
)
from runtime_manager.distribution.signing import (
    SignatureVerificationError,
    sign_manifest,
)


def _make_keypair():
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


def _skill_dict(**overrides):
    base = {
        "name": "deploy",
        "version": 8,
        "sha256": "a" * 64,
        "size_bytes": 28400,
        "download_url": "/skills/deploy/8/bundle.tar.gz",
        "targets": ["box"],
    }
    base.update(overrides)
    return base


def _manifest_dict(**overrides):
    base = {
        "schema_version": 1,
        "distributor_id": "acme-skills",
        "client_id": "client-42",
        "manifest_version": 14,
        "updated_at": "2026-04-21T10:00:00Z",
        "skills": [_skill_dict()],
    }
    base.update(overrides)
    return base


def _to_bytes(data):
    return json.dumps(data).encode("utf-8")


class TestParseManifest(unittest.TestCase):

    def test_valid_manifest(self) -> None:
        m = parse_manifest(_to_bytes(_manifest_dict()))
        self.assertEqual(m.schema_version, 1)
        self.assertEqual(m.distributor_id, "acme-skills")
        self.assertEqual(m.client_id, "client-42")
        self.assertEqual(m.manifest_version, 14)
        self.assertEqual(m.updated_at, "2026-04-21T10:00:00Z")
        self.assertEqual(len(m.skills), 1)
        self.assertEqual(m.skills[0].name, "deploy")
        self.assertEqual(m.skills[0].version, 8)
        self.assertEqual(m.skills[0].targets, ["box"])

    def test_multiple_skills(self) -> None:
        skills = [
            _skill_dict(name="deploy", targets=["box"]),
            _skill_dict(name="audit", version=3, targets=["box", "laptop"]),
        ]
        m = parse_manifest(_to_bytes(_manifest_dict(skills=skills)))
        self.assertEqual(len(m.skills), 2)
        self.assertEqual(m.skills[0].name, "deploy")
        self.assertEqual(m.skills[1].name, "audit")
        self.assertEqual(m.skills[1].targets, ["box", "laptop"])

    def test_skill_optional_fields(self) -> None:
        skill = _skill_dict(
            min_version=7,
            min_version_reason="CVE-2026-xxxx",
            changelog="Fixed rollback bug",
        )
        m = parse_manifest(_to_bytes(_manifest_dict(skills=[skill])))
        s = m.skills[0]
        self.assertEqual(s.min_version, 7)
        self.assertEqual(s.min_version_reason, "CVE-2026-xxxx")
        self.assertEqual(s.changelog, "Fixed rollback bug")

    def test_skill_missing_optional_fields(self) -> None:
        m = parse_manifest(_to_bytes(_manifest_dict()))
        s = m.skills[0]
        self.assertIsNone(s.min_version)
        self.assertIsNone(s.min_version_reason)
        self.assertIsNone(s.changelog)

    def test_raw_dict_preserved(self) -> None:
        data = _manifest_dict()
        m = parse_manifest(_to_bytes(data))
        self.assertEqual(m.raw, data)

    def test_invalid_json(self) -> None:
        with self.assertRaises(ManifestSchemaError) as ctx:
            parse_manifest(b"not json{")
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_not_an_object(self) -> None:
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(b"[1,2,3]")

    def test_unsupported_schema_version(self) -> None:
        data = _manifest_dict(schema_version=99)
        with self.assertRaises(ManifestSchemaError) as ctx:
            parse_manifest(_to_bytes(data))
        self.assertIn("unsupported schema_version", str(ctx.exception))

    def test_missing_schema_version(self) -> None:
        data = _manifest_dict()
        del data["schema_version"]
        with self.assertRaises(ManifestSchemaError) as ctx:
            parse_manifest(_to_bytes(data))
        self.assertIn("schema_version", str(ctx.exception))

    def test_missing_distributor_id(self) -> None:
        data = _manifest_dict()
        del data["distributor_id"]
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(_to_bytes(data))

    def test_missing_client_id(self) -> None:
        data = _manifest_dict()
        del data["client_id"]
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(_to_bytes(data))

    def test_missing_manifest_version(self) -> None:
        data = _manifest_dict()
        del data["manifest_version"]
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(_to_bytes(data))

    def test_missing_updated_at(self) -> None:
        data = _manifest_dict()
        del data["updated_at"]
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(_to_bytes(data))

    def test_missing_skills(self) -> None:
        data = _manifest_dict()
        del data["skills"]
        with self.assertRaises(ManifestSchemaError) as ctx:
            parse_manifest(_to_bytes(data))
        self.assertIn("skills", str(ctx.exception))

    def test_skills_not_a_list(self) -> None:
        data = _manifest_dict(skills="oops")
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(_to_bytes(data))

    def test_skill_not_an_object(self) -> None:
        data = _manifest_dict(skills=["not-a-dict"])
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(_to_bytes(data))

    def test_skill_missing_required_name(self) -> None:
        skill = _skill_dict()
        del skill["name"]
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(_to_bytes(_manifest_dict(skills=[skill])))

    def test_skill_missing_required_version(self) -> None:
        skill = _skill_dict()
        del skill["version"]
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(_to_bytes(_manifest_dict(skills=[skill])))

    def test_skill_missing_required_sha256(self) -> None:
        skill = _skill_dict()
        del skill["sha256"]
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(_to_bytes(_manifest_dict(skills=[skill])))

    def test_skill_missing_required_download_url(self) -> None:
        skill = _skill_dict()
        del skill["download_url"]
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(_to_bytes(_manifest_dict(skills=[skill])))

    def test_skill_missing_targets(self) -> None:
        skill = _skill_dict()
        del skill["targets"]
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(_to_bytes(_manifest_dict(skills=[skill])))

    def test_skill_targets_not_a_list(self) -> None:
        skill = _skill_dict(targets="box")
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(_to_bytes(_manifest_dict(skills=[skill])))

    def test_empty_skills_list(self) -> None:
        m = parse_manifest(_to_bytes(_manifest_dict(skills=[])))
        self.assertEqual(m.skills, [])

    def test_boolean_rejected_as_integer(self) -> None:
        data = _manifest_dict(manifest_version=True)
        with self.assertRaises(ManifestSchemaError):
            parse_manifest(_to_bytes(data))


class TestVerifyManifest(unittest.TestCase):

    def test_valid_signature_passes(self) -> None:
        private, public = _make_keypair()
        data = _manifest_dict()
        signed = sign_manifest(data, private)
        manifest = parse_manifest(_to_bytes(signed))
        verify_manifest(manifest, public)

    def test_tampered_manifest_fails(self) -> None:
        private, public = _make_keypair()
        data = _manifest_dict()
        signed = sign_manifest(data, private)
        signed["manifest_version"] = 999
        manifest = parse_manifest(_to_bytes(signed))
        with self.assertRaises(SignatureVerificationError):
            verify_manifest(manifest, public)

    def test_wrong_key_fails(self) -> None:
        private1, _ = _make_keypair()
        _, public2 = _make_keypair()
        data = _manifest_dict()
        signed = sign_manifest(data, private1)
        manifest = parse_manifest(_to_bytes(signed))
        with self.assertRaises(SignatureVerificationError):
            verify_manifest(manifest, public2)

    def test_missing_signature_fails(self) -> None:
        _, public = _make_keypair()
        manifest = parse_manifest(_to_bytes(_manifest_dict()))
        with self.assertRaises(SignatureVerificationError):
            verify_manifest(manifest, public)


class TestFilterSkillsForTarget(unittest.TestCase):

    def _make_manifest(self, skills):
        data = _manifest_dict(skills=skills)
        return parse_manifest(_to_bytes(data))

    def test_filters_by_target(self) -> None:
        skills = [
            _skill_dict(name="deploy", targets=["box"]),
            _skill_dict(name="audit", targets=["laptop"]),
            _skill_dict(name="security", targets=["box", "laptop"]),
        ]
        m = self._make_manifest(skills)

        box_skills = filter_skills_for_target(m, "box")
        self.assertEqual([s.name for s in box_skills], ["deploy", "security"])

        laptop_skills = filter_skills_for_target(m, "laptop")
        self.assertEqual([s.name for s in laptop_skills], ["audit", "security"])

    def test_empty_targets_matches_nothing(self) -> None:
        skills = [_skill_dict(name="orphan", targets=[])]
        m = self._make_manifest(skills)
        self.assertEqual(filter_skills_for_target(m, "box"), [])
        self.assertEqual(filter_skills_for_target(m, "laptop"), [])

    def test_unknown_target_returns_empty(self) -> None:
        skills = [_skill_dict(name="deploy", targets=["box"])]
        m = self._make_manifest(skills)
        self.assertEqual(filter_skills_for_target(m, "server"), [])

    def test_all_match(self) -> None:
        skills = [
            _skill_dict(name="a", targets=["box"]),
            _skill_dict(name="b", targets=["box"]),
        ]
        m = self._make_manifest(skills)
        self.assertEqual(len(filter_skills_for_target(m, "box")), 2)

    def test_empty_manifest_skills(self) -> None:
        m = self._make_manifest([])
        self.assertEqual(filter_skills_for_target(m, "box"), [])


if __name__ == "__main__":
    unittest.main()
