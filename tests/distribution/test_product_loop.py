"""Product-loop smoke for local publish, preview, sync, inspect, and rollback."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
TESTS_DIST_DIR = Path(__file__).resolve().parent
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))
if str(TESTS_DIST_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIST_DIR))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runtime_manager.distribution.lockfile import emit_lockfile, parse_lockfile
from runtime_manager.distribution.preview import preview_manifest
from runtime_manager.distribution.publish import publish_skill_release
from runtime_manager.distribution.rollback import rollback_distributor_skill
from runtime_manager.distribution.signing import public_key_to_config_str
from runtime_manager.distribution.sync import sync_distributor_set

from test_sync_pipeline import (
    TEST_API_KEY,
    TEST_CLIENT_ID,
    TEST_DISTRIBUTOR_ID,
    _make_distributor_config,
    _make_source,
    _start_server,
    _stop_server,
)


def _write_private_key(path: Path, key: Ed25519PrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


class TestDistributionProductLoop(unittest.TestCase):

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
        self.lockfile_path = self.tmpdir / "skill-repos.lock.json"
        self.install_targets = [{"id": "default", "host_path": str(self.install_root)}]
        self.server = None

    def tearDown(self) -> None:
        if self.server is not None:
            _stop_server(self.server)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _publish(self, version: int, changelog: str):
        return publish_skill_release(
            skill_path=self.skill,
            version=version,
            manifest_path=self.manifest_path,
            artifact_root=self.artifact_root,
            signing_key_ref=f"file:{self.key_path}",
            distributor_id=TEST_DISTRIBUTOR_ID,
            client_id=TEST_CLIENT_ID,
            targets=["box"],
            capabilities=["deploy"],
            changelog=changelog,
        )

    def _server_bundles(self, *payloads) -> dict[str, bytes]:
        return {
            payload["download_url"]: Path(payload["artifact_path"]).read_bytes()
            for payload in payloads
        }

    def _write_lockfile(self, entries, manifest_entry) -> None:
        self.lockfile_path.write_text(
            json.dumps(
                emit_lockfile(
                    entries,
                    {TEST_DISTRIBUTOR_ID: manifest_entry},
                    config_sha="product-loop",
                    synced_at=manifest_entry.fetched_at,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_publish_preview_sync_update_and_rollback(self) -> None:
        v1 = self._publish(1, "first release")
        self.server, base_url = _start_server(
            self.manifest_path.read_bytes(),
            self._server_bundles(v1),
        )
        config = _make_distributor_config(base_url, self.public_key_config)
        source = _make_source(pick=["deploy"])

        preview1 = preview_manifest(
            manifest_bytes=self.manifest_path.read_bytes(),
            public_key_config=self.public_key_config,
            distributor_id=TEST_DISTRIBUTOR_ID,
            state_root=self.state_root,
            pick=["deploy"],
            lockfile_path=self.lockfile_path,
        )
        self.assertTrue(preview1["ready"])
        self.assertEqual(preview1["items"][0]["action"], "install")

        entries1, manifest_entry1 = sync_distributor_set(
            config,
            source,
            self.state_root,
            self.install_targets,
        )
        self.assertIsNotNone(manifest_entry1)
        self._write_lockfile(entries1, manifest_entry1)
        self.assertIn(
            "version 1",
            (self.install_root / "deploy" / "SKILL.md").read_text(encoding="utf-8"),
        )

        (self.skill / "SKILL.md").write_text("# deploy\n\nversion 2\n", encoding="utf-8")
        v2 = self._publish(2, "second release")
        self.server.manifest_bytes = self.manifest_path.read_bytes()  # type: ignore[attr-defined]
        self.server.bundles = self._server_bundles(v1, v2)  # type: ignore[attr-defined]

        preview2 = preview_manifest(
            manifest_bytes=self.manifest_path.read_bytes(),
            public_key_config=self.public_key_config,
            distributor_id=TEST_DISTRIBUTOR_ID,
            state_root=self.state_root,
            pick=["deploy"],
            lockfile_path=self.lockfile_path,
        )
        self.assertTrue(preview2["ready"])
        self.assertEqual(preview2["items"][0]["action"], "update")
        self.assertEqual(preview2["items"][0]["selected_version"], 2)

        entries2, manifest_entry2 = sync_distributor_set(
            config,
            source,
            self.state_root,
            self.install_targets,
            existing_manifest_version=manifest_entry1.manifest_version,
        )
        self.assertIsNotNone(manifest_entry2)
        self._write_lockfile(entries2, manifest_entry2)
        lock_after_update = parse_lockfile(json.loads(self.lockfile_path.read_text(encoding="utf-8")))
        self.assertEqual(lock_after_update.skills[0].version, 2)
        self.assertIn(
            "version 2",
            (self.install_root / "deploy" / "SKILL.md").read_text(encoding="utf-8"),
        )

        rollback = rollback_distributor_skill(
            manifest_path=self.manifest_path,
            public_key_config=self.public_key_config,
            distributor_id=TEST_DISTRIBUTOR_ID,
            skill_name="deploy",
            target_version=1,
            state_root=self.state_root,
            install_targets=self.install_targets,
            lockfile_path=self.lockfile_path,
            reason="bad second release",
        )

        self.assertEqual(rollback["from_version"], 2)
        self.assertEqual(rollback["to_version"], 1)
        lock_after_rollback = parse_lockfile(json.loads(self.lockfile_path.read_text(encoding="utf-8")))
        self.assertEqual(lock_after_rollback.skills[0].version, 1)
        self.assertEqual(lock_after_rollback.skills[0].pinned_by, "rollback")
        self.assertIn(
            "version 1",
            (self.install_root / "deploy" / "SKILL.md").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
