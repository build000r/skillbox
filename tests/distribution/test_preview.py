"""Tests for read-only distribution preview plans."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runtime_manager.distribution.lockfile import (
    DistributorManifestLockEntry,
    SkillLockEntry,
    emit_lockfile,
)
from runtime_manager.distribution.preview import preview_manifest
from runtime_manager.distribution.signing import public_key_to_config_str, sign_manifest


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_bytes(private_key: Ed25519PrivateKey, *, artifacts=None, **skill_overrides) -> bytes:
    if artifacts is None:
        artifacts = [
            {
                "version": 6,
                "sha256": _sha(b"six"),
                "size_bytes": 3,
                "download_url": "/skills/deploy/6/bundle.tar.gz",
                "changelog": "v6",
            },
            {
                "version": 8,
                "sha256": _sha(b"eight"),
                "size_bytes": 5,
                "download_url": "/skills/deploy/8/bundle.tar.gz",
                "changelog": "v8",
            },
        ]
    skill = {
        "name": "deploy",
        "recommended_version": 8,
        "targets": ["box"],
        "capabilities": ["deploy"],
        "artifacts": artifacts,
    }
    skill.update(skill_overrides)
    manifest = {
        "schema_version": 2,
        "distributor_id": "local-dist",
        "client_id": "client-42",
        "manifest_version": 1,
        "updated_at": "2026-05-04T00:00:00Z",
        "skills": [skill],
    }
    signed = sign_manifest(manifest, private_key)
    return json.dumps(signed, indent=2).encode("utf-8")


class TestDistributionPreview(unittest.TestCase):

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.private = Ed25519PrivateKey.generate()
        self.public_key_config = public_key_to_config_str(self.private.public_key())
        self.state_root = self.tmpdir / "state"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _preview(self, manifest_bytes: bytes | None = None, **overrides):
        params = dict(
            manifest_bytes=manifest_bytes or _manifest_bytes(self.private),
            public_key_config=self.public_key_config,
            distributor_id="local-dist",
            state_root=self.state_root,
        )
        params.update(overrides)
        return preview_manifest(**params)

    def test_preview_install_is_ready_and_does_not_write_state(self) -> None:
        payload = self._preview()

        self.assertTrue(payload["ready"])
        self.assertFalse(self.state_root.exists())
        item = payload["items"][0]
        self.assertEqual(item["action"], "install")
        self.assertEqual(item["selected_version"], 8)
        self.assertEqual(item["artifact_sha256"], _sha(b"eight"))
        self.assertEqual(item["cache_state"], "missing")
        self.assertEqual(item["signature_state"], "verified")

    def test_preview_unchanged_when_lockfile_matches_cached_artifact(self) -> None:
        cache_dir = self.state_root / "bundle-cache" / "deploy"
        cache_dir.mkdir(parents=True)
        (cache_dir / "deploy-v8.skillbundle.tar.gz").write_bytes(b"eight")
        lockfile_path = self.tmpdir / "lock.json"
        lockfile_path.write_text(
            json.dumps(
                emit_lockfile(
                    [
                        SkillLockEntry(
                            name="deploy",
                            source="distributor",
                            distributor_id="local-dist",
                            version=8,
                            bundle_sha256=_sha(b"eight"),
                            pinned_by="manifest_recommendation",
                        )
                    ],
                    {
                        "local-dist": DistributorManifestLockEntry(
                            manifest_version=1,
                            fetched_at="2026-05-04T00:00:00Z",
                            signature_verified=True,
                        )
                    },
                    config_sha="abc",
                    synced_at="2026-05-04T00:00:00Z",
                )
            ),
            encoding="utf-8",
        )

        payload = self._preview(lockfile_path=lockfile_path)

        item = payload["items"][0]
        self.assertEqual(item["action"], "unchanged")
        self.assertEqual(item["cache_state"], "cached")

    def test_pin_below_floor_is_visible(self) -> None:
        payload = self._preview(
            manifest_bytes=_manifest_bytes(
                self.private,
                min_version=6,
                min_version_reason="security floor",
            ),
            pin={"deploy": 5},
        )

        item = payload["items"][0]
        self.assertEqual(item["action"], "floor_override")
        self.assertEqual(item["selected_version"], 6)
        self.assertEqual(item["pinned_by"], "manifest_floor")
        self.assertEqual(item["pin_reason"], "security floor")

    def test_missing_selected_artifact_blocks(self) -> None:
        payload = self._preview(
            manifest_bytes=_manifest_bytes(
                self.private,
                artifacts=[
                    {
                        "version": 8,
                        "sha256": _sha(b"eight"),
                        "size_bytes": 5,
                        "download_url": "/skills/deploy/8/bundle.tar.gz",
                    },
                ],
            ),
            pin={"deploy": 6},
        )

        self.assertFalse(payload["ready"])
        item = payload["items"][0]
        self.assertEqual(item["action"], "blocked")
        self.assertIn("DISTRIBUTION_ARTIFACT_NOT_AVAILABLE", item["warnings"][0])

    def test_invalid_signature_blocks_before_plan(self) -> None:
        raw = json.loads(_manifest_bytes(self.private))
        raw["manifest_version"] = 999

        payload = self._preview(manifest_bytes=json.dumps(raw).encode("utf-8"))

        self.assertFalse(payload["ready"])
        self.assertEqual(payload["signature_state"], "invalid")
        self.assertEqual(payload["items"], [])

    def test_malformed_public_key_blocks_before_plan(self) -> None:
        payload = self._preview(public_key_config="ed25519:not-valid-base64!!!")

        self.assertFalse(payload["ready"])
        self.assertEqual(payload["signature_state"], "invalid")
        self.assertEqual(payload["items"], [])
        self.assertIn("manifest public key invalid", payload["warnings"][0])


if __name__ == "__main__":
    unittest.main()
