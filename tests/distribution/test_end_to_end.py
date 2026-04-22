"""End-to-end integration smoke test for skillbox distribution (WG-009).

Exercises the full pipeline: config → sync → install → lockfile → doctor.
Uses a real local HTTP server — no mocks, no network.
"""
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

from runtime_manager.distribution.doctor import validate_distribution_doctor_checks
from runtime_manager.distribution.lockfile import (
    DistributorManifestLockEntry,
    SkillLockEntry,
    emit_lockfile,
    parse_lockfile,
)
from runtime_manager.distribution.status import (
    collect_distributor_status,
    render_connected_distributors_section,
)
from runtime_manager.distribution.sync import sync_distributor_set
from runtime_manager.shared_distribution import (
    DistributorAuth,
    DistributorConfig,
    DistributorSetSource,
    DistributorVerification,
)

from fixture_server import (
    TEST_API_KEY,
    TEST_CLIENT_ID,
    TEST_DIST_KEY_ENV,
    TEST_DISTRIBUTOR_ID,
    start_test_distributor_server,
)


class TestEndToEndDistribution(unittest.TestCase):
    """Full pipeline: HTTP server → sync → install → lockfile → doctor → status."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.fixture = start_test_distributor_server(self.tmpdir / "server")

        self.repo_root = self.tmpdir / "repo"
        self.state_root = self.repo_root / ".skillbox-state"
        self.install_root = self.tmpdir / "install" / "skills"
        self.install_root.mkdir(parents=True)

        self.config_path = self.repo_root / "workspace" / "skill-repos.yaml"
        self.lock_path = self.repo_root / "workspace" / "skill-repos.lock.json"

        self._write_config()

    def tearDown(self) -> None:
        self.fixture.shutdown()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            "version: 2\n"
            "distributors:\n"
            f"  - id: {TEST_DISTRIBUTOR_ID}\n"
            f"    url: {self.fixture.url}\n"
            f"    client_id: {TEST_CLIENT_ID}\n"
            "    auth:\n"
            "      method: api-key\n"
            f"      key_env: {TEST_DIST_KEY_ENV}\n"
            "    verification:\n"
            f"      public_key: \"{self.fixture.public_key_str}\"\n"
            "skill_repos:\n"
            f"  - distributor: {TEST_DISTRIBUTOR_ID}\n"
            "    pick: [deploy]\n",
            encoding="utf-8",
        )

    def _distributor_config(self) -> DistributorConfig:
        return DistributorConfig(
            id=TEST_DISTRIBUTOR_ID,
            url=self.fixture.url,
            client_id=TEST_CLIENT_ID,
            auth=DistributorAuth(method="api-key", key_env=TEST_DIST_KEY_ENV),
            verification=DistributorVerification(public_key=self.fixture.public_key_str),
        )

    def _source(self) -> DistributorSetSource:
        return DistributorSetSource(
            distributor=TEST_DISTRIBUTOR_ID,
            pick=["deploy"],
            pin={},
        )

    def _install_targets(self) -> list[dict[str, str]]:
        return [{"id": "default", "host_path": str(self.install_root)}]

    def _model(self) -> dict:
        return {
            "root_dir": str(self.repo_root),
            "skills": [
                {
                    "kind": "skill-repo-set",
                    "id": "test-skills",
                    "skill_repos_config_host_path": str(self.config_path),
                    "lock_path_host_path": str(self.lock_path),
                    "sync": {"mode": "clone-and-install"},
                    "install_targets": self._install_targets(),
                }
            ],
        }

    def _write_lockfile(
        self,
        entries: list[SkillLockEntry],
        manifest_entry: DistributorManifestLockEntry | None,
    ) -> None:
        manifests = {}
        if manifest_entry is not None:
            manifests[TEST_DISTRIBUTOR_ID] = manifest_entry

        from runtime_manager.shared import file_sha256
        config_sha = file_sha256(self.config_path) if self.config_path.is_file() else ""

        payload = emit_lockfile(
            entries,
            manifests,
            config_sha=config_sha,
            synced_at=manifest_entry.fetched_at if manifest_entry else "2026-04-21T00:00:00Z",
        )
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @mock.patch.dict(os.environ, {TEST_DIST_KEY_ENV: TEST_API_KEY})
    def test_full_pipeline_sync_install_lockfile_doctor(self) -> None:
        config = self._distributor_config()
        source = self._source()

        # -- Phase 1: Sync (real HTTP, real crypto, real disk I/O) --------
        entries, manifest_entry = sync_distributor_set(
            config, source, self.state_root, self._install_targets(),
        )

        # -- Phase 2: Verify installation --------------------------------
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.name, "deploy")
        self.assertEqual(entry.source, "distributor")
        self.assertEqual(entry.distributor_id, TEST_DISTRIBUTOR_ID)
        self.assertEqual(entry.version, 8)
        self.assertEqual(entry.pinned_by, "manifest_recommendation")
        self.assertIsNotNone(entry.bundle_sha256)
        self.assertIsNotNone(entry.install_tree_sha)
        self.assertIsNotNone(entry.bundle_tree_sha256)

        installed_skill = self.install_root / "deploy"
        self.assertTrue(installed_skill.is_dir())
        self.assertTrue((installed_skill / "SKILL.md").is_file())
        self.assertTrue((installed_skill / "references" / "guide.md").is_file())
        self.assertFalse(
            (installed_skill / ".skill-meta").exists(),
            ".skill-meta should be stripped during install",
        )

        # -- Phase 3: Verify manifest cache ------------------------------
        self.assertIsNotNone(manifest_entry)
        self.assertEqual(manifest_entry.manifest_version, 14)
        self.assertTrue(manifest_entry.signature_verified)

        cached_manifest = self.state_root / "manifests" / f"{TEST_DISTRIBUTOR_ID}.json"
        self.assertTrue(cached_manifest.is_file())

        cached_bundle = (
            self.state_root / "bundle-cache" / "deploy"
            / "deploy-v8.skillbundle.tar.gz"
        )
        self.assertTrue(cached_bundle.is_file())

        # -- Phase 4: Write lockfile and verify shape --------------------
        self._write_lockfile(entries, manifest_entry)

        raw_lock = json.loads(self.lock_path.read_text(encoding="utf-8"))
        self.assertEqual(raw_lock["version"], 3)
        self.assertIn(TEST_DISTRIBUTOR_ID, raw_lock["distributor_manifests"])
        dm = raw_lock["distributor_manifests"][TEST_DISTRIBUTOR_ID]
        self.assertEqual(dm["manifest_version"], 14)
        self.assertTrue(dm["signature_verified"])

        lock_skills = raw_lock["skills"]
        self.assertEqual(len(lock_skills), 1)
        ls = lock_skills[0]
        self.assertEqual(ls["name"], "deploy")
        self.assertEqual(ls["source"], "distributor")
        self.assertEqual(ls["distributor_id"], TEST_DISTRIBUTOR_ID)
        self.assertEqual(ls["version"], 8)
        self.assertEqual(ls["bundle_sha256"], entry.bundle_sha256)
        self.assertEqual(ls["pinned_by"], "manifest_recommendation")

        parsed_lock = parse_lockfile(raw_lock)
        self.assertEqual(len(parsed_lock.skills), 1)
        self.assertEqual(parsed_lock.skills[0].source, "distributor")

        # -- Phase 5: Doctor checks (all 5 should pass) -----------------
        model = self._model()
        checks = validate_distribution_doctor_checks(model)
        by_code = {c.code: c for c in checks}

        expected_codes = {
            "distributor_config_valid",
            "distributor_auth_probe",
            "distributor_manifest_signature",
            "distributor_bundle_cache_integrity",
            "distributor_lockfile_consistency",
        }
        self.assertEqual(set(by_code.keys()), expected_codes)

        for code in expected_codes:
            check = by_code[code]
            self.assertIn(
                check.status, ("pass",),
                f"doctor check {code!r} expected pass, got {check.status}: {check.message}"
                + (f" details={check.details}" if check.details else ""),
            )

        # -- Phase 6: Status surface ------------------------------------
        distributor_rows = collect_distributor_status(model)
        self.assertEqual(len(distributor_rows), 1)
        row = distributor_rows[0]
        self.assertEqual(row["id"], TEST_DISTRIBUTOR_ID)
        self.assertEqual(row["client_id"], TEST_CLIENT_ID)
        self.assertEqual(row["skills_count"], 1)
        self.assertEqual(row["manifest_version"], 14)
        self.assertTrue(row["auth_key_present"])

        context_lines = render_connected_distributors_section(model)
        self.assertTrue(len(context_lines) > 0)
        context_text = "\n".join(context_lines)
        self.assertIn("Connected Distributors", context_text)
        self.assertIn(TEST_DISTRIBUTOR_ID, context_text)

    @mock.patch.dict(os.environ, {TEST_DIST_KEY_ENV: TEST_API_KEY})
    def test_idempotent_sync_short_circuits_on_same_manifest_version(self) -> None:
        config = self._distributor_config()
        source = self._source()

        entries_1, manifest_1 = sync_distributor_set(
            config, source, self.state_root, self._install_targets(),
        )
        self.assertEqual(len(entries_1), 1)
        self.assertIsNotNone(manifest_1)

        entries_2, manifest_2 = sync_distributor_set(
            config, source, self.state_root, self._install_targets(),
            existing_manifest_version=manifest_1.manifest_version,
        )
        self.assertEqual(entries_2, [])
        self.assertIsNone(manifest_2)

    @mock.patch.dict(os.environ, {TEST_DIST_KEY_ENV: TEST_API_KEY})
    def test_bundle_cache_hit_skips_download(self) -> None:
        config = self._distributor_config()
        source = self._source()

        sync_distributor_set(
            config, source, self.state_root, self._install_targets(),
        )

        install_2 = self.tmpdir / "install2" / "skills"
        install_2.mkdir(parents=True)
        targets_2 = [{"id": "second", "host_path": str(install_2)}]

        entries_2, _ = sync_distributor_set(
            config, source, self.state_root, targets_2,
        )
        self.assertEqual(len(entries_2), 1)
        self.assertTrue((install_2 / "deploy" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
