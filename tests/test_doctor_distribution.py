from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

HELPERS = SourceFileLoader(
    "runtime_manager_test_helpers_for_distribution_doctor",
    str((ROOT_DIR / "tests" / "test_runtime_manager.py").resolve()),
).load_module()

from runtime_manager.distribution.doctor import validate_distribution_doctor_checks  # noqa: E402
from runtime_manager.distribution.lockfile import (  # noqa: E402
    DistributorManifestLockEntry,
    SkillLockEntry,
    emit_lockfile,
)
from runtime_manager.distribution.signing import (  # noqa: E402
    public_key_to_config_str,
    sign_manifest,
)


DIST_ID = "acme-skills"
DIST_KEY_ENV = "ACME_DISTRIBUTOR_KEY"


def _write_distributor_config(repo: Path, public_key: str, *, distributor_id: str = DIST_ID) -> None:
    (repo / "workspace" / "skill-repos.yaml").write_text(
        "version: 2\n"
        "distributors:\n"
        f"  - id: {distributor_id}\n"
        "    url: http://127.0.0.1:65530/api/v1\n"
        "    client_id: client-42\n"
        "    auth:\n"
        "      method: api-key\n"
        f"      key_env: {DIST_KEY_ENV}\n"
        "    verification:\n"
        f"      public_key: \"{public_key}\"\n"
        "skill_repos:\n"
        f"  - distributor: {distributor_id}\n"
        "    pick: [deploy]\n"
        "    pin:\n"
        "      deploy: 8\n",
        encoding="utf-8",
    )


def _signed_manifest_bytes(
    private_key: Ed25519PrivateKey,
    *,
    distributor_id: str = DIST_ID,
    bundle_sha: str = "a" * 64,
) -> bytes:
    payload = {
        "schema_version": 1,
        "distributor_id": distributor_id,
        "client_id": "client-42",
        "manifest_version": 14,
        "updated_at": "2026-04-21T10:00:00Z",
        "skills": [
            {
                "name": "deploy",
                "version": 8,
                "sha256": bundle_sha,
                "size_bytes": 12,
                "download_url": "/skills/deploy/8/bundle.tar.gz",
                "targets": ["box"],
            }
        ],
    }
    signed = sign_manifest(payload, private_key)
    return json.dumps(signed, indent=2).encode("utf-8")


def _write_manifest_cache(repo: Path, manifest_bytes: bytes, *, distributor_id: str = DIST_ID) -> Path:
    manifest_path = repo / ".skillbox-state" / "manifests" / f"{distributor_id}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest_bytes)
    return manifest_path


def _write_distribution_lockfile(
    repo: Path,
    *,
    distributor_id: str = DIST_ID,
    bundle_sha: str = "a" * 64,
    include_manifest_entry: bool = True,
) -> Path:
    lock_path = repo / "workspace" / "skill-repos.lock.json"
    manifests = (
        {
            distributor_id: DistributorManifestLockEntry(
                manifest_version=14,
                fetched_at="2026-04-21T10:00:05Z",
                signature_verified=True,
            )
        }
        if include_manifest_entry
        else {}
    )
    payload = emit_lockfile(
        [
            SkillLockEntry(
                name="deploy",
                source="distributor",
                distributor_id=distributor_id,
                version=8,
                bundle_sha256=bundle_sha,
                pinned_by="manifest_recommendation",
            )
        ],
        manifests,
        config_sha="cfg",
        synced_at="2026-04-21T10:00:10Z",
    )
    lock_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return lock_path


def _write_bundle_cache(repo: Path, bundle_bytes: bytes, *, version: int = 8) -> Path:
    bundle_path = (
        repo
        / ".skillbox-state"
        / "bundle-cache"
        / "deploy"
        / f"deploy-v{version}.skillbundle.tar.gz"
    )
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_bytes(bundle_bytes)
    return bundle_path


class DistributionDoctorChecksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.helpers = HELPERS.RuntimeManagerTests(methodName="runTest")

    def _model(self, repo: Path) -> dict:
        return HELPERS.MANAGE_MODULE.build_runtime_model(repo)

    def test_no_distributor_config_reports_pass_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self.helpers._write_fixture(repo)

            result = self.helpers._run(repo, "doctor", "--format", "json")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            by_code = {item["code"]: item for item in payload["checks"]}

            required_codes = {
                "distributor_config_valid",
                "distributor_auth_probe",
                "distributor_manifest_signature",
                "distributor_bundle_cache_integrity",
                "distributor_lockfile_consistency",
            }
            self.assertTrue(required_codes.issubset(set(by_code)), payload["checks"])
            for code in required_codes:
                self.assertEqual(by_code[code]["status"], "pass", payload["checks"])
                self.assertIn("no distributor configured", by_code[code]["message"])

    def test_distributor_config_check_warns_when_auth_env_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self.helpers._write_fixture(repo)

            private_key = Ed25519PrivateKey.generate()
            _write_distributor_config(repo, public_key_to_config_str(private_key.public_key()))
            model = self._model(repo)

            with mock.patch.dict(os.environ, {}, clear=True):
                checks = validate_distribution_doctor_checks(model)

            by_code = {item.code: item for item in checks}
            config_check = by_code["distributor_config_valid"]
            self.assertEqual(config_check.status, "warn")
            self.assertIn(DIST_KEY_ENV, " ".join(config_check.details.get("issues", [])))

    def test_distributor_config_check_fails_on_dangling_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self.helpers._write_fixture(repo)

            (repo / "workspace" / "skill-repos.yaml").write_text(
                "version: 2\n"
                "distributors:\n"
                "  - id: known-dist\n"
                "    url: http://127.0.0.1:65530/api/v1\n"
                "    client_id: client-42\n"
                "    auth:\n"
                "      method: api-key\n"
                f"      key_env: {DIST_KEY_ENV}\n"
                "    verification:\n"
                "      public_key: \"ed25519:abc123\"\n"
                "skill_repos:\n"
                "  - distributor: unknown-dist\n"
                "    pick: [deploy]\n",
                encoding="utf-8",
            )
            model = self._model(repo)

            checks = validate_distribution_doctor_checks(model)
            by_code = {item.code: item for item in checks}
            config_check = by_code["distributor_config_valid"]
            self.assertEqual(config_check.status, "fail")
            self.assertIn("unknown distributor", " ".join(config_check.details.get("issues", [])))

    def test_auth_probe_warns_when_endpoint_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self.helpers._write_fixture(repo)
            private_key = Ed25519PrivateKey.generate()
            _write_distributor_config(repo, public_key_to_config_str(private_key.public_key()))
            model = self._model(repo)

            with mock.patch.dict(os.environ, {DIST_KEY_ENV: "token"}, clear=False):
                checks = validate_distribution_doctor_checks(model)

            by_code = {item.code: item for item in checks}
            self.assertEqual(by_code["distributor_auth_probe"].status, "warn")

    def test_manifest_signature_check_fails_for_tampered_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self.helpers._write_fixture(repo)

            private_key = Ed25519PrivateKey.generate()
            _write_distributor_config(repo, public_key_to_config_str(private_key.public_key()))
            manifest_bytes = _signed_manifest_bytes(private_key)
            tampered = json.loads(manifest_bytes)
            tampered["signature"] = "ed25519:" + base64.b64encode(b"\x00" * 64).decode("ascii")
            _write_manifest_cache(repo, json.dumps(tampered, indent=2).encode("utf-8"))
            model = self._model(repo)

            with mock.patch.dict(os.environ, {DIST_KEY_ENV: "token"}, clear=False):
                checks = validate_distribution_doctor_checks(model)

            by_code = {item.code: item for item in checks}
            signature_check = by_code["distributor_manifest_signature"]
            self.assertEqual(signature_check.status, "fail")
            self.assertIn("signature verification failed", " ".join(signature_check.details.get("issues", [])))

    def test_bundle_cache_integrity_warns_on_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self.helpers._write_fixture(repo)

            private_key = Ed25519PrivateKey.generate()
            _write_distributor_config(repo, public_key_to_config_str(private_key.public_key()))
            _write_manifest_cache(repo, _signed_manifest_bytes(private_key, bundle_sha="b" * 64))
            _write_distribution_lockfile(repo, bundle_sha="b" * 64)
            _write_bundle_cache(repo, b"bundle-bytes-that-do-not-match")
            model = self._model(repo)

            with mock.patch.dict(os.environ, {DIST_KEY_ENV: "token"}, clear=False):
                checks = validate_distribution_doctor_checks(model)

            by_code = {item.code: item for item in checks}
            cache_check = by_code["distributor_bundle_cache_integrity"]
            self.assertEqual(cache_check.status, "warn")
            self.assertIn("hash mismatch", " ".join(cache_check.details.get("issues", [])))

    def test_lockfile_consistency_warns_when_manifest_reference_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self.helpers._write_fixture(repo)

            private_key = Ed25519PrivateKey.generate()
            _write_distributor_config(repo, public_key_to_config_str(private_key.public_key()))
            _write_distribution_lockfile(repo, include_manifest_entry=True)
            model = self._model(repo)

            with mock.patch.dict(os.environ, {DIST_KEY_ENV: "token"}, clear=False):
                checks = validate_distribution_doctor_checks(model)

            by_code = {item.code: item for item in checks}
            lock_check = by_code["distributor_lockfile_consistency"]
            self.assertEqual(lock_check.status, "warn")
            self.assertIn("cached manifest is missing", " ".join(lock_check.details.get("issues", [])))

    def test_doctor_exits_nonzero_when_manifest_signature_is_tampered(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            self.helpers._write_fixture(repo)

            private_key = Ed25519PrivateKey.generate()
            _write_distributor_config(repo, public_key_to_config_str(private_key.public_key()))
            manifest_bytes = _signed_manifest_bytes(private_key)
            tampered = json.loads(manifest_bytes)
            tampered["signature"] = "ed25519:AA=="
            _write_manifest_cache(repo, json.dumps(tampered, indent=2).encode("utf-8"))

            with mock.patch.dict(os.environ, {DIST_KEY_ENV: "token"}, clear=False):
                result = self.helpers._run(repo, "doctor", "--format", "json")

            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            failures = [
                item for item in payload["checks"]
                if item["status"] == "fail" and item["code"] == "distributor_manifest_signature"
            ]
            self.assertEqual(len(failures), 1, payload["checks"])


if __name__ == "__main__":
    unittest.main()
