"""Tests for local distribution publisher primitives."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runtime_manager.distribution.manifest import parse_manifest, verify_manifest
from runtime_manager.distribution.publish import (
    DISTRIBUTION_SKILL_METADATA_MISSING,
    DISTRIBUTION_VERSION_CONFLICT,
    DistributionPublishError,
    publish_skill_release,
)
from runtime_manager.distribution.signing import public_key_to_config_str, load_public_key


def _write_private_key(path: Path, key: Ed25519PrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _make_skill(root: Path, name: str = "deploy") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"# {name}\n\nA test skill.\n", encoding="utf-8")
    (skill / "reference.md").write_text("details\n", encoding="utf-8")
    return skill


class TestDistributionPublish(unittest.TestCase):

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.private = Ed25519PrivateKey.generate()
        self.public_key_config = public_key_to_config_str(self.private.public_key())
        self.key_path = self.tmpdir / "private.pem"
        _write_private_key(self.key_path, self.private)
        self.skill = _make_skill(self.tmpdir / "src")
        self.manifest_path = self.tmpdir / "manifest.json"
        self.artifact_root = self.tmpdir / "artifacts"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _publish(self, **overrides):
        params = dict(
            skill_path=self.skill,
            version=8,
            manifest_path=self.manifest_path,
            artifact_root=self.artifact_root,
            signing_key_ref=f"file:{self.key_path}",
            distributor_id="local-dist",
            client_id="client-42",
            targets=["box"],
            capabilities=["deploy"],
            changelog="Initial release",
        )
        params.update(overrides)
        return publish_skill_release(**params)

    def test_publish_creates_signed_v2_manifest_and_artifact(self) -> None:
        payload = self._publish()

        self.assertEqual(payload["result"], "published")
        artifact_path = Path(payload["artifact_path"])
        self.assertTrue(artifact_path.is_file())
        self.assertEqual(payload["size_bytes"], artifact_path.stat().st_size)

        manifest = parse_manifest(self.manifest_path.read_bytes())
        verify_manifest(manifest, load_public_key(self.public_key_config))
        self.assertEqual(manifest.schema_version, 2)
        self.assertEqual(manifest.distributor_id, "local-dist")
        self.assertEqual(manifest.skills[0].recommended_version, 8)
        self.assertEqual(manifest.skills[0].artifacts[0].sha256, payload["artifact_sha256"])
        self.assertEqual(manifest.skills[0].capabilities, ["deploy"])

    def test_republishing_same_version_and_same_bytes_is_noop(self) -> None:
        first = self._publish()
        second = self._publish()

        self.assertEqual(second["result"], "noop")
        self.assertEqual(second["artifact_sha256"], first["artifact_sha256"])
        raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["manifest_version"], 1)

    def test_same_version_different_bytes_is_conflict(self) -> None:
        self._publish()
        (self.skill / "reference.md").write_text("changed\n", encoding="utf-8")

        with self.assertRaises(DistributionPublishError) as ctx:
            self._publish()
        self.assertIn(DISTRIBUTION_VERSION_CONFLICT, str(ctx.exception))

    def test_missing_skill_metadata_returns_stable_error(self) -> None:
        os.remove(self.skill / "SKILL.md")

        with self.assertRaises(DistributionPublishError) as ctx:
            self._publish()
        self.assertIn(DISTRIBUTION_SKILL_METADATA_MISSING, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
