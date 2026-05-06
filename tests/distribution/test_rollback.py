"""Tests for distribution rollback from the signed bundle cache."""
from __future__ import annotations

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

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runtime_manager.distribution.lockfile import (
    DistributorManifestLockEntry,
    SkillLockEntry,
    emit_lockfile,
    parse_lockfile,
)
from runtime_manager.distribution.publish import publish_skill_release
from runtime_manager.distribution.rollback import (
    DISTRIBUTION_CACHE_MISSING,
    DISTRIBUTION_FLOOR_VIOLATION,
    DistributionRollbackError,
    rollback_distributor_skill,
)
from runtime_manager.distribution.signing import public_key_to_config_str


def _write_private_key(path: Path, key: Ed25519PrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


class TestDistributionRollback(unittest.TestCase):

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.private = Ed25519PrivateKey.generate()
        self.public_key_config = public_key_to_config_str(self.private.public_key())
        self.key_path = self.tmpdir / "private.pem"
        _write_private_key(self.key_path, self.private)

        self.skill = self.tmpdir / "src" / "deploy"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("# deploy\n\nversion 1\n", encoding="utf-8")
        self.manifest_path = self.tmpdir / "manifest.json"
        self.artifact_root = self.tmpdir / "artifacts"
        self.state_root = self.tmpdir / "state"
        self.install_root = self.tmpdir / "install"
        self.lockfile_path = self.tmpdir / "lock.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _publish(self, version: int, **overrides):
        params = dict(
            skill_path=self.skill,
            version=version,
            manifest_path=self.manifest_path,
            artifact_root=self.artifact_root,
            signing_key_ref=f"file:{self.key_path}",
            distributor_id="local-dist",
            client_id="client-42",
            targets=["box"],
        )
        params.update(overrides)
        return publish_skill_release(**params)

    def _seed_two_versions(self, *, min_version: int | None = None):
        v1 = self._publish(1, changelog="v1")
        (self.skill / "SKILL.md").write_text("# deploy\n\nversion 2\n", encoding="utf-8")
        v2 = self._publish(2, changelog="v2", min_version=min_version)
        return v1, v2

    def _cache_artifact(self, version: int, payload) -> None:
        cache_dir = self.state_root / "bundle-cache" / "deploy"
        cache_dir.mkdir(parents=True)
        shutil.copyfile(
            payload["artifact_path"],
            cache_dir / f"deploy-v{version}.skillbundle.tar.gz",
        )

    def _write_lockfile(self, version: int, sha256: str) -> None:
        self.lockfile_path.write_text(
            json.dumps(
                emit_lockfile(
                    [
                        SkillLockEntry(
                            name="deploy",
                            source="distributor",
                            distributor_id="local-dist",
                            version=version,
                            bundle_sha256=sha256,
                            pinned_by="manifest_recommendation",
                        )
                    ],
                    {
                        "local-dist": DistributorManifestLockEntry(
                            manifest_version=2,
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

    def test_rollback_installs_cached_version_and_updates_lockfile(self) -> None:
        v1, v2 = self._seed_two_versions()
        self._cache_artifact(1, v1)
        self._write_lockfile(2, v2["artifact_sha256"])

        payload = rollback_distributor_skill(
            manifest_path=self.manifest_path,
            public_key_config=self.public_key_config,
            distributor_id="local-dist",
            skill_name="deploy",
            target_version=1,
            state_root=self.state_root,
            install_targets=[{"id": "default", "host_path": str(self.install_root)}],
            lockfile_path=self.lockfile_path,
            reason="bad update",
        )

        self.assertTrue(payload["lockfile_updated"])
        self.assertEqual(payload["to_version"], 1)
        self.assertEqual(payload["pinned_by"], "rollback")
        self.assertIn(1, payload["cached_versions"])
        installed = (self.install_root / "deploy" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("version 1", installed)

        lock = parse_lockfile(json.loads(self.lockfile_path.read_text(encoding="utf-8")))
        self.assertEqual(lock.skills[0].version, 1)
        self.assertEqual(lock.skills[0].pinned_by, "rollback")
        self.assertEqual(lock.skills[0].pin_reason, "bad update")

    def test_missing_cache_is_stable_error(self) -> None:
        self._seed_two_versions()

        with self.assertRaises(DistributionRollbackError) as ctx:
            rollback_distributor_skill(
                manifest_path=self.manifest_path,
                public_key_config=self.public_key_config,
                distributor_id="local-dist",
                skill_name="deploy",
                target_version=1,
                state_root=self.state_root,
                install_targets=[{"id": "default", "host_path": str(self.install_root)}],
                lockfile_path=self.lockfile_path,
            )
        self.assertIn(DISTRIBUTION_CACHE_MISSING, str(ctx.exception))

    def test_malformed_public_key_is_stable_error(self) -> None:
        self._seed_two_versions()

        with self.assertRaises(DistributionRollbackError) as ctx:
            rollback_distributor_skill(
                manifest_path=self.manifest_path,
                public_key_config="ed25519:not-valid-base64!!!",
                distributor_id="local-dist",
                skill_name="deploy",
                target_version=1,
                state_root=self.state_root,
                install_targets=[{"id": "default", "host_path": str(self.install_root)}],
                lockfile_path=self.lockfile_path,
            )
        self.assertIn("manifest signature verification failed", str(ctx.exception))

    def test_floor_violation_requires_override(self) -> None:
        v1, _ = self._seed_two_versions(min_version=2)
        self._cache_artifact(1, v1)

        with self.assertRaises(DistributionRollbackError) as ctx:
            rollback_distributor_skill(
                manifest_path=self.manifest_path,
                public_key_config=self.public_key_config,
                distributor_id="local-dist",
                skill_name="deploy",
                target_version=1,
                state_root=self.state_root,
                install_targets=[{"id": "default", "host_path": str(self.install_root)}],
                lockfile_path=self.lockfile_path,
            )
        self.assertIn(DISTRIBUTION_FLOOR_VIOLATION, str(ctx.exception))

    def test_floor_violation_can_be_overridden_and_recorded(self) -> None:
        v1, _ = self._seed_two_versions(min_version=2)
        self._cache_artifact(1, v1)

        rollback_distributor_skill(
            manifest_path=self.manifest_path,
            public_key_config=self.public_key_config,
            distributor_id="local-dist",
            skill_name="deploy",
            target_version=1,
            state_root=self.state_root,
            install_targets=[{"id": "default", "host_path": str(self.install_root)}],
            lockfile_path=self.lockfile_path,
            emergency_override=True,
        )

        lock = parse_lockfile(json.loads(self.lockfile_path.read_text(encoding="utf-8")))
        self.assertTrue(lock.skills[0].extras["rollback_emergency_override"])


if __name__ == "__main__":
    unittest.main()
