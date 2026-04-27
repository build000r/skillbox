"""Tests for the distributor-set sync pipeline (WG-006).

Uses a real local HTTP server to serve mock manifests and bundles.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))
if str(ROOT_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "scripts"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runtime_manager.distribution.bundle import pack_skill_bundle, _file_sha256
from runtime_manager.distribution.lockfile import (
    DistributorManifestLockEntry,
    Lockfile,
    SkillLockEntry,
    emit_lockfile,
    parse_lockfile,
)
from runtime_manager.distribution.signing import (
    public_key_to_config_str,
    sign_detached,
    sign_manifest,
)
from runtime_manager.distribution.sync import (
    DistributorSyncError,
    sync_distributor_set,
    sync_distributor_sources,
)
import runtime_manager.distribution.sync as sync_module
from runtime_manager.shared_distribution import (
    DistributorAuth,
    DistributorConfig,
    DistributorSetSource,
    DistributorVerification,
)


# ---------------------------------------------------------------------------
# Test fixtures — dynamically generate signed manifests and bundles
# ---------------------------------------------------------------------------

TEST_API_KEY = "test-api-key-42"
TEST_CLIENT_ID = "test-client"
TEST_DISTRIBUTOR_ID = "test-dist"


def _make_keypair():
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    return private, public


def _make_skill_dir(root: Path, name: str = "deploy") -> Path:
    skill = root / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"# {name}\n\nA test skill.\n")
    refs = skill / "references"
    refs.mkdir()
    (refs / "guide.md").write_text("## Guide\n\nUsage info.\n")
    return skill


def _make_bundle_and_manifest(
    tmpdir: Path,
    private_key: Ed25519PrivateKey,
    *,
    skill_name: str = "deploy",
    version: int = 8,
    min_version: int | None = None,
    min_version_reason: str | None = None,
    targets: list[str] | None = None,
    manifest_version: int = 14,
) -> tuple[bytes, bytes, str]:
    """Build a signed bundle and signed manifest.

    Returns ``(manifest_bytes, bundle_bytes, bundle_sha256)``.
    """
    skill_dir = _make_skill_dir(tmpdir / "src", skill_name)
    bundle_output = tmpdir / "bundles"
    bundle_output.mkdir(parents=True, exist_ok=True)
    bundle_path = pack_skill_bundle(
        skill_dir, version, name=skill_name, output_dir=bundle_output,
    )
    bundle_bytes = bundle_path.read_bytes()
    bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()

    skill_entry: dict[str, Any] = {
        "name": skill_name,
        "version": version,
        "sha256": bundle_sha,
        "size_bytes": len(bundle_bytes),
        "download_url": f"/skills/{skill_name}/{version}/bundle.tar.gz",
        "targets": targets or ["box"],
    }
    if min_version is not None:
        skill_entry["min_version"] = min_version
    if min_version_reason is not None:
        skill_entry["min_version_reason"] = min_version_reason

    manifest_dict: dict[str, Any] = {
        "schema_version": 1,
        "distributor_id": TEST_DISTRIBUTOR_ID,
        "client_id": TEST_CLIENT_ID,
        "manifest_version": manifest_version,
        "updated_at": "2026-04-21T10:00:00Z",
        "skills": [skill_entry],
    }
    signed_manifest = sign_manifest(manifest_dict, private_key)
    manifest_bytes = json.dumps(signed_manifest, indent=2).encode("utf-8")

    return manifest_bytes, bundle_bytes, bundle_sha


# ---------------------------------------------------------------------------
# Local HTTP test server
# ---------------------------------------------------------------------------

class _MockDistributorHandler(http.server.BaseHTTPRequestHandler):
    """Serves manifest and bundles from ``self.server`` attributes."""

    def do_GET(self) -> None:
        server = self.server

        expected_key = getattr(server, "api_key", "")
        if expected_key:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {expected_key}":
                self.send_error(401, "Unauthorized")
                return

        if getattr(server, "fail_code", 0):
            self.send_error(server.fail_code, "Simulated failure")
            return

        if self.path == "/manifest":
            body = getattr(server, "manifest_bytes", b"")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        bundles = getattr(server, "bundles", {})
        if self.path in bundles:
            body = bundles[self.path]
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_error(404)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass


def _start_server(
    manifest_bytes: bytes,
    bundles: dict[str, bytes],
    *,
    api_key: str = TEST_API_KEY,
    fail_code: int = 0,
) -> tuple[http.server.HTTPServer, str]:
    """Start a local HTTP server and return ``(server, base_url)``."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _MockDistributorHandler)
    server.manifest_bytes = manifest_bytes  # type: ignore[attr-type]
    server.bundles = bundles  # type: ignore[attr-type]
    server.api_key = api_key  # type: ignore[attr-type]
    server.fail_code = fail_code  # type: ignore[attr-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


# ---------------------------------------------------------------------------
# Helper to build DistributorConfig for tests
# ---------------------------------------------------------------------------

def _make_distributor_config(
    base_url: str,
    public_key_str: str,
    *,
    key_env: str = "TEST_DIST_KEY",
) -> DistributorConfig:
    return DistributorConfig(
        id=TEST_DISTRIBUTOR_ID,
        url=base_url,
        client_id=TEST_CLIENT_ID,
        auth=DistributorAuth(method="api-key", key_env=key_env),
        verification=DistributorVerification(public_key=public_key_str),
    )


def _make_source(
    pick: list[str] | None = None,
    pin: dict[str, int] | None = None,
) -> DistributorSetSource:
    return DistributorSetSource(
        distributor=TEST_DISTRIBUTOR_ID,
        pick=pick or [],
        pin=pin or {},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSyncDistributorSetHappyPath(unittest.TestCase):
    """Full happy-path: fetch manifest, download bundle, install skill."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.private, self.public = _make_keypair()
        self.pub_str = public_key_to_config_str(self.public)

        self.manifest_bytes, self.bundle_bytes, self.bundle_sha = (
            _make_bundle_and_manifest(self.tmpdir / "gen", self.private)
        )
        self.server, self.base_url = _start_server(
            self.manifest_bytes,
            {"/skills/deploy/8/bundle.tar.gz": self.bundle_bytes},
        )
        self.state_root = self.tmpdir / "state"
        self.install_root = self.tmpdir / "install"
        self.install_root.mkdir(parents=True)
        self.install_targets = [{"id": "default", "host_path": str(self.install_root)}]

    def tearDown(self) -> None:
        self.server.shutdown()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_happy_path_installs_skill_and_returns_lock_entries(self) -> None:
        config = _make_distributor_config(self.base_url, self.pub_str)
        source = _make_source()

        entries, manifest_entry = sync_distributor_set(
            config, source, self.state_root, self.install_targets,
        )

        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.name, "deploy")
        self.assertEqual(entry.source, "distributor")
        self.assertEqual(entry.distributor_id, TEST_DISTRIBUTOR_ID)
        self.assertEqual(entry.version, 8)
        self.assertEqual(entry.bundle_sha256, self.bundle_sha)
        self.assertEqual(entry.pinned_by, "manifest_recommendation")
        self.assertIsNotNone(entry.install_tree_sha)
        self.assertIsNotNone(entry.bundle_tree_sha256)

        self.assertIsNotNone(manifest_entry)
        self.assertEqual(manifest_entry.manifest_version, 14)
        self.assertTrue(manifest_entry.signature_verified)

        installed_skill = self.install_root / "deploy" / "SKILL.md"
        self.assertTrue(installed_skill.is_file())

        meta_dir = self.install_root / "deploy" / ".skill-meta"
        self.assertFalse(meta_dir.exists())

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_manifest_cached_to_state_root(self) -> None:
        config = _make_distributor_config(self.base_url, self.pub_str)
        sync_distributor_set(
            config, _make_source(), self.state_root, self.install_targets,
        )
        manifest_cache = self.state_root / "manifests" / f"{TEST_DISTRIBUTOR_ID}.json"
        self.assertTrue(manifest_cache.is_file())
        cached = json.loads(manifest_cache.read_bytes())
        self.assertEqual(cached["manifest_version"], 14)

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_bundle_cached_to_state_root(self) -> None:
        config = _make_distributor_config(self.base_url, self.pub_str)
        sync_distributor_set(
            config, _make_source(), self.state_root, self.install_targets,
        )
        cached = self.state_root / "bundle-cache" / "deploy" / "deploy-v8.skillbundle.tar.gz"
        self.assertTrue(cached.is_file())


class TestSyncDistributorSetSignatureFailure(unittest.TestCase):
    """Signature verification failure aborts without installing."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        priv, pub = _make_keypair()

        manifest_bytes, self.bundle_bytes, self.bundle_sha = (
            _make_bundle_and_manifest(self.tmpdir / "gen", priv)
        )

        tampered = json.loads(manifest_bytes)
        tampered["signature"] = "ed25519:" + base64.b64encode(b"\x00" * 64).decode()
        self.manifest_bytes = json.dumps(tampered).encode()

        self.server, self.base_url = _start_server(
            self.manifest_bytes,
            {"/skills/deploy/8/bundle.tar.gz": self.bundle_bytes},
        )
        self.state_root = self.tmpdir / "state"
        self.install_root = self.tmpdir / "install"
        self.install_root.mkdir(parents=True)
        self.install_targets = [{"id": "default", "host_path": str(self.install_root)}]

        _, self.other_pub = _make_keypair()
        self.pub_str = public_key_to_config_str(pub)

    def tearDown(self) -> None:
        self.server.shutdown()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_bad_signature_raises_and_no_install(self) -> None:
        config = _make_distributor_config(self.base_url, self.pub_str)
        with self.assertRaises(DistributorSyncError) as ctx:
            sync_distributor_set(
                config, _make_source(), self.state_root, self.install_targets,
            )
        self.assertIn("signature", str(ctx.exception).lower())
        self.assertFalse((self.install_root / "deploy").exists())


class TestSyncDistributorSetNetworkError(unittest.TestCase):
    """Network errors surface as DistributorSyncError."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        _, self.public = _make_keypair()
        self.pub_str = public_key_to_config_str(self.public)
        self.state_root = self.tmpdir / "state"
        self.install_targets = [{"id": "default", "host_path": str(self.tmpdir / "install")}]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_server_error_raises(self) -> None:
        server, base_url = _start_server(b"", {}, fail_code=500)
        try:
            config = _make_distributor_config(base_url, self.pub_str)
            with self.assertRaises(DistributorSyncError) as ctx:
                sync_distributor_set(
                    config, _make_source(), self.state_root, self.install_targets,
                )
            self.assertIn("500", str(ctx.exception))
        finally:
            server.shutdown()

    def test_missing_api_key_raises(self) -> None:
        config = _make_distributor_config("http://localhost:1", self.pub_str,
                                          key_env="NONEXISTENT_KEY_VAR")
        with self.assertRaises(DistributorSyncError) as ctx:
            sync_distributor_set(
                config, _make_source(), self.state_root, self.install_targets,
            )
        self.assertIn("NONEXISTENT_KEY_VAR", str(ctx.exception))


class TestSyncDistributorSetPinResolution(unittest.TestCase):
    """Pin resolution is wired correctly through the sync pipeline."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.private, self.public = _make_keypair()
        self.pub_str = public_key_to_config_str(self.public)

        self.manifest_bytes, self.bundle_bytes, self.bundle_sha = (
            _make_bundle_and_manifest(
                self.tmpdir / "gen", self.private,
                version=8, min_version=7,
                min_version_reason="CVE-2026-xxxx",
            )
        )
        self.server, self.base_url = _start_server(
            self.manifest_bytes,
            {"/skills/deploy/8/bundle.tar.gz": self.bundle_bytes},
        )
        self.state_root = self.tmpdir / "state"
        self.install_root = self.tmpdir / "install"
        self.install_root.mkdir(parents=True)
        self.install_targets = [{"id": "default", "host_path": str(self.install_root)}]

    def tearDown(self) -> None:
        self.server.shutdown()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_client_pin_below_floor_resolves_to_floor(self) -> None:
        """Client pins v6, but min_version is 7 → installs v7."""
        config = _make_distributor_config(self.base_url, self.pub_str)
        source = _make_source(pin={"deploy": 6})

        entries, _ = sync_distributor_set(
            config, source, self.state_root, self.install_targets,
        )
        self.assertEqual(entries[0].version, 7)
        self.assertEqual(entries[0].pinned_by, "manifest_floor")
        self.assertEqual(entries[0].pin_reason, "CVE-2026-xxxx")

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_no_client_pin_resolves_to_recommended(self) -> None:
        """No client pin → installs recommended version (8)."""
        config = _make_distributor_config(self.base_url, self.pub_str)
        source = _make_source()

        entries, _ = sync_distributor_set(
            config, source, self.state_root, self.install_targets,
        )
        self.assertEqual(entries[0].version, 8)
        self.assertEqual(entries[0].pinned_by, "manifest_recommendation")


class TestSyncDistributorSetTargetFilter(unittest.TestCase):
    """Target env filter drops non-matching skills."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.private, self.public = _make_keypair()
        self.pub_str = public_key_to_config_str(self.public)

        self.manifest_bytes, self.bundle_bytes, self.bundle_sha = (
            _make_bundle_and_manifest(
                self.tmpdir / "gen", self.private, targets=["box"],
            )
        )
        self.server, self.base_url = _start_server(
            self.manifest_bytes,
            {"/skills/deploy/8/bundle.tar.gz": self.bundle_bytes},
        )
        self.state_root = self.tmpdir / "state"
        self.install_root = self.tmpdir / "install"
        self.install_root.mkdir(parents=True)
        self.install_targets = [{"id": "default", "host_path": str(self.install_root)}]

    def tearDown(self) -> None:
        self.server.shutdown()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_target_mismatch_skips_skill(self) -> None:
        config = _make_distributor_config(self.base_url, self.pub_str)
        entries, manifest_entry = sync_distributor_set(
            config, _make_source(), self.state_root, self.install_targets,
            target_env="laptop",
        )
        self.assertEqual(len(entries), 0)
        self.assertIsNotNone(manifest_entry)
        self.assertFalse((self.install_root / "deploy").exists())

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_target_match_installs_skill(self) -> None:
        config = _make_distributor_config(self.base_url, self.pub_str)
        entries, _ = sync_distributor_set(
            config, _make_source(), self.state_root, self.install_targets,
            target_env="box",
        )
        self.assertEqual(len(entries), 1)
        self.assertTrue((self.install_root / "deploy" / "SKILL.md").is_file())


class TestSyncDistributorSetCacheHit(unittest.TestCase):
    """Pre-cached bundle with matching hash skips download."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.private, self.public = _make_keypair()
        self.pub_str = public_key_to_config_str(self.public)

        self.manifest_bytes, self.bundle_bytes, self.bundle_sha = (
            _make_bundle_and_manifest(self.tmpdir / "gen", self.private)
        )
        self.state_root = self.tmpdir / "state"
        self.install_root = self.tmpdir / "install"
        self.install_root.mkdir(parents=True)
        self.install_targets = [{"id": "default", "host_path": str(self.install_root)}]

        cache_dir = self.state_root / "bundle-cache" / "deploy"
        cache_dir.mkdir(parents=True)
        (cache_dir / "deploy-v8.skillbundle.tar.gz").write_bytes(self.bundle_bytes)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_cache_hit_does_not_download(self) -> None:
        """Server only serves manifest (no bundle route); cache hit works."""
        server, base_url = _start_server(self.manifest_bytes, {})
        try:
            config = _make_distributor_config(base_url, self.pub_str)
            entries, manifest_entry = sync_distributor_set(
                config, _make_source(), self.state_root, self.install_targets,
            )
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].name, "deploy")
            self.assertTrue((self.install_root / "deploy" / "SKILL.md").is_file())
        finally:
            server.shutdown()


class TestSyncDistributorSetIdempotency(unittest.TestCase):
    """Unchanged manifest_version triggers short-circuit."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.private, self.public = _make_keypair()
        self.pub_str = public_key_to_config_str(self.public)

        self.manifest_bytes, self.bundle_bytes, _ = (
            _make_bundle_and_manifest(self.tmpdir / "gen", self.private,
                                      manifest_version=14)
        )
        self.server, self.base_url = _start_server(
            self.manifest_bytes,
            {"/skills/deploy/8/bundle.tar.gz": self.bundle_bytes},
        )
        self.state_root = self.tmpdir / "state"
        self.install_root = self.tmpdir / "install"
        self.install_root.mkdir(parents=True)
        self.install_targets = [{"id": "default", "host_path": str(self.install_root)}]

    def tearDown(self) -> None:
        self.server.shutdown()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_unchanged_manifest_version_is_noop(self) -> None:
        config = _make_distributor_config(self.base_url, self.pub_str)
        entries, manifest_entry = sync_distributor_set(
            config, _make_source(), self.state_root, self.install_targets,
            existing_manifest_version=14,
        )
        self.assertEqual(entries, [])
        self.assertIsNone(manifest_entry)
        self.assertFalse((self.install_root / "deploy").exists())

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_different_manifest_version_proceeds(self) -> None:
        config = _make_distributor_config(self.base_url, self.pub_str)
        entries, manifest_entry = sync_distributor_set(
            config, _make_source(), self.state_root, self.install_targets,
            existing_manifest_version=13,
        )
        self.assertEqual(len(entries), 1)
        self.assertIsNotNone(manifest_entry)


class TestSyncDistributorSetPickFilter(unittest.TestCase):
    """Pick list filters manifest skills."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.private, self.public = _make_keypair()
        self.pub_str = public_key_to_config_str(self.public)

        self.manifest_bytes, self.bundle_bytes, _ = (
            _make_bundle_and_manifest(self.tmpdir / "gen", self.private,
                                      skill_name="deploy")
        )
        self.server, self.base_url = _start_server(
            self.manifest_bytes,
            {"/skills/deploy/8/bundle.tar.gz": self.bundle_bytes},
        )
        self.state_root = self.tmpdir / "state"
        self.install_root = self.tmpdir / "install"
        self.install_root.mkdir(parents=True)
        self.install_targets = [{"id": "default", "host_path": str(self.install_root)}]

    def tearDown(self) -> None:
        self.server.shutdown()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_pick_excludes_unmatched_skills(self) -> None:
        config = _make_distributor_config(self.base_url, self.pub_str)
        source = _make_source(pick=["other-skill"])
        entries, manifest_entry = sync_distributor_set(
            config, source, self.state_root, self.install_targets,
        )
        self.assertEqual(len(entries), 0)
        self.assertIsNotNone(manifest_entry)

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_pick_includes_matched_skills(self) -> None:
        config = _make_distributor_config(self.base_url, self.pub_str)
        source = _make_source(pick=["deploy"])
        entries, _ = sync_distributor_set(
            config, source, self.state_root, self.install_targets,
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "deploy")


class TestSyncDistributorSources(unittest.TestCase):
    """Integration test for the model-level orchestrator."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.private, self.public = _make_keypair()
        self.pub_str = public_key_to_config_str(self.public)

        self.manifest_bytes, self.bundle_bytes, self.bundle_sha = (
            _make_bundle_and_manifest(self.tmpdir / "gen", self.private)
        )
        self.server, self.base_url = _start_server(
            self.manifest_bytes,
            {"/skills/deploy/8/bundle.tar.gz": self.bundle_bytes},
        )

        self.root_dir = self.tmpdir / "repo"
        self.root_dir.mkdir()
        self.config_dir = self.root_dir / "workspace"
        self.config_dir.mkdir()
        self.config_path = self.config_dir / "skill-repos.yaml"
        self.lock_path = self.root_dir / ".skillbox-state" / "skill-repos.lock"
        self.lock_path.parent.mkdir(parents=True)
        self.install_root = self.root_dir / ".skillbox-state" / "skills" / "default"
        self.install_root.mkdir(parents=True)

        import yaml
        config_data = {
            "version": 2,
            "distributors": [{
                "id": TEST_DISTRIBUTOR_ID,
                "url": self.base_url,
                "client_id": TEST_CLIENT_ID,
                "auth": {"method": "api-key", "key_env": "TEST_DIST_KEY"},
                "verification": {"public_key": self.pub_str},
            }],
            "skill_repos": [{
                "distributor": TEST_DISTRIBUTOR_ID,
                "pick": ["deploy"],
            }],
        }
        self.config_path.write_text(
            yaml.dump(config_data, default_flow_style=False),
            encoding="utf-8",
        )

        self.model = {
            "root_dir": str(self.root_dir),
            "skills": [{
                "kind": "skill-repo-set",
                "sync": {"mode": "clone-and-install"},
                "skill_repos_config_host_path": str(self.config_path),
                "lock_path_host_path": str(self.lock_path),
                "clone_root_host_path": str(self.root_dir / "clones"),
                "install_targets": [{
                    "id": "default",
                    "host_path": str(self.install_root),
                }],
            }],
        }

    def tearDown(self) -> None:
        self.server.shutdown()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_model_level_sync_writes_lockfile(self) -> None:
        actions = sync_distributor_sources(self.model, dry_run=False)

        install_actions = [a for a in actions if "install-distributor-skill" in a]
        self.assertTrue(len(install_actions) >= 1)

        self.assertTrue(self.lock_path.is_file())
        raw = json.loads(self.lock_path.read_text(encoding="utf-8"))
        lock = parse_lockfile(raw)

        self.assertEqual(lock.version, 3)
        dist_skills = [s for s in lock.skills if s.source == "distributor"]
        self.assertEqual(len(dist_skills), 1)
        self.assertEqual(dist_skills[0].name, "deploy")

        self.assertIn(TEST_DISTRIBUTOR_ID, lock.distributor_manifests)
        me = lock.distributor_manifests[TEST_DISTRIBUTOR_ID]
        self.assertEqual(me.manifest_version, 14)

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_model_level_sync_uses_configured_state_root_for_cache(self) -> None:
        custom_state_root = self.tmpdir / "custom-state"
        model = dict(self.model)
        model["storage"] = {"state_root": str(custom_state_root)}

        sync_distributor_sources(model, dry_run=False)

        self.assertTrue((custom_state_root / "manifests" / f"{TEST_DISTRIBUTOR_ID}.json").is_file())
        self.assertTrue(
            (
                custom_state_root
                / "bundle-cache"
                / "deploy"
                / "deploy-v8.skillbundle.tar.gz"
            ).is_file()
        )
        self.assertFalse((self.root_dir / ".skillbox-state" / "manifests").exists())

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_model_level_sync_merges_with_existing_repo_entries(self) -> None:
        """Existing repo-sourced entries in lockfile are preserved after merge."""
        existing_lock = {
            "version": 2,
            "config_sha": "abc123",
            "synced_at": "2026-04-20T00:00:00Z",
            "skills": [{
                "name": "audit-plans",
                "source": "repo",
                "repo": "build000r/skills",
                "declared_ref": "main",
                "resolved_commit": "deadbeef",
                "install_tree_sha": "treehash123",
            }],
        }
        self.lock_path.write_text(
            json.dumps(existing_lock, indent=2) + "\n", encoding="utf-8",
        )

        sync_distributor_sources(self.model, dry_run=False)

        raw = json.loads(self.lock_path.read_text(encoding="utf-8"))
        lock = parse_lockfile(raw)

        repo_skills = [s for s in lock.skills if s.source == "repo"]
        dist_skills = [s for s in lock.skills if s.source == "distributor"]
        self.assertEqual(len(repo_skills), 1)
        self.assertEqual(repo_skills[0].name, "audit-plans")
        self.assertEqual(len(dist_skills), 1)
        self.assertEqual(dist_skills[0].name, "deploy")


class TestSyncDistributorSourcesCoverageEdges(TestSyncDistributorSources):
    """Targeted branch coverage tests for sync_distributor_sources/_carry_forward."""

    def test_skips_non_skill_repo_sets_wrong_modes_and_bad_config_paths(self) -> None:
        model = {
            "root_dir": str(self.root_dir),
            "skills": [
                {"kind": "packaged-skill-set"},
                {
                    "kind": "skill-repo-set",
                    "sync": {"mode": "manual"},
                    "skill_repos_config_host_path": str(self.config_path),
                    "lock_path_host_path": str(self.lock_path),
                },
                {
                    "kind": "skill-repo-set",
                    "sync": {"mode": "clone-and-install"},
                    "skill_repos_config_host_path": str(self.root_dir / "workspace" / "missing.yaml"),
                    "lock_path_host_path": str(self.lock_path),
                },
            ],
        }

        actions = sync_distributor_sources(model, dry_run=False)
        self.assertEqual(actions, [])

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_skips_when_distribution_sources_are_not_declared(self) -> None:
        import yaml

        no_dist_config = {
            "version": 2,
            "skill_repos": [{"repo": "build000r/skills", "ref": "main"}],
        }
        self.config_path.write_text(
            yaml.dump(no_dist_config, default_flow_style=False),
            encoding="utf-8",
        )

        actions = sync_distributor_sources(self.model, dry_run=False)
        self.assertEqual(actions, [])

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_invalid_existing_lock_and_sync_error_do_not_write_lock(self) -> None:
        self.lock_path.write_text("{not-json", encoding="utf-8")

        with mock.patch.object(
            sync_module,
            "sync_distributor_set",
            side_effect=DistributorSyncError("simulated failure"),
        ):
            actions = sync_distributor_sources(self.model, dry_run=False)

        self.assertTrue(
            any(action.startswith("distributor-sync-error:") for action in actions),
            actions,
        )
        self.assertFalse(
            any(action.startswith("write-lockfile:") for action in actions),
            actions,
        )

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_manifest_unchanged_without_existing_lock_skips_merge_write(self) -> None:
        if self.lock_path.exists():
            self.lock_path.unlink()

        with mock.patch.object(
            sync_module,
            "sync_distributor_set",
            return_value=([], None),
        ):
            actions = sync_distributor_sources(self.model, dry_run=False)

        self.assertIn(f"distributor-unchanged: {TEST_DISTRIBUTOR_ID}", actions)
        self.assertFalse(
            any(action.startswith("write-lockfile:") for action in actions),
            actions,
        )
        self.assertFalse(self.lock_path.exists())

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_manifest_unchanged_carries_forward_matching_dist_and_preserves_non_dist_entries(self) -> None:
        existing_payload = emit_lockfile(
            [
                SkillLockEntry(
                    name="audit-plans",
                    source="repo",
                    repo="build000r/skills",
                    declared_ref="main",
                    resolved_commit="deadbeef",
                    install_tree_sha="tree-repo",
                ),
                SkillLockEntry(
                    name="local-path-skill",
                    source="repo",
                    source_path="./skills/local-path-skill",
                    install_tree_sha="tree-path",
                ),
                SkillLockEntry(
                    name="deploy",
                    source="distributor",
                    distributor_id=TEST_DISTRIBUTOR_ID,
                    version=8,
                    bundle_sha256="sha-deploy",
                ),
                # Legacy distributor entry without distributor_id should not be carried forward.
                SkillLockEntry(
                    name="legacy-dist-entry",
                    source="distributor",
                    version=1,
                    bundle_sha256="sha-legacy",
                ),
                SkillLockEntry(
                    name="other-dist-entry",
                    source="distributor",
                    distributor_id="other-dist",
                    version=2,
                    bundle_sha256="sha-other",
                ),
            ],
            {
                TEST_DISTRIBUTOR_ID: DistributorManifestLockEntry(
                    manifest_version=14,
                    fetched_at="2026-04-21T10:00:00Z",
                    signature_verified=True,
                ),
                "other-dist": DistributorManifestLockEntry(
                    manifest_version=5,
                    fetched_at="2026-04-21T09:00:00Z",
                    signature_verified=True,
                ),
            },
            config_sha="cfg-existing",
            synced_at="2026-04-21T10:00:10Z",
        )
        self.lock_path.write_text(
            json.dumps(existing_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with mock.patch.object(
            sync_module,
            "sync_distributor_set",
            return_value=([], None),
        ) as sync_mock:
            actions = sync_distributor_sources(self.model, dry_run=False)

        self.assertEqual(sync_mock.call_count, 1)
        self.assertEqual(sync_mock.call_args.kwargs["existing_manifest_version"], 14)
        self.assertIn(f"distributor-unchanged: {TEST_DISTRIBUTOR_ID}", actions)
        self.assertTrue(
            any(action.startswith("write-lockfile:") for action in actions),
            actions,
        )

        lock = parse_lockfile(json.loads(self.lock_path.read_text(encoding="utf-8")))
        self.assertEqual(lock.config_sha, "cfg-existing")

        names = [entry.name for entry in lock.skills]
        self.assertIn("audit-plans", names)
        self.assertIn("local-path-skill", names)
        self.assertIn("deploy", names)
        self.assertNotIn("legacy-dist-entry", names)
        self.assertNotIn("other-dist-entry", names)

        deploy_entry = next(entry for entry in lock.skills if entry.name == "deploy")
        self.assertEqual(deploy_entry.distributor_id, TEST_DISTRIBUTOR_ID)

        self.assertIn(TEST_DISTRIBUTOR_ID, lock.distributor_manifests)
        self.assertIn("other-dist", lock.distributor_manifests)


class TestSyncDistributorSetDryRun(unittest.TestCase):
    """Dry-run mode returns lock entries without installing."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.private, self.public = _make_keypair()
        self.pub_str = public_key_to_config_str(self.public)

        self.manifest_bytes, self.bundle_bytes, _ = (
            _make_bundle_and_manifest(self.tmpdir / "gen", self.private)
        )
        self.server, self.base_url = _start_server(
            self.manifest_bytes,
            {"/skills/deploy/8/bundle.tar.gz": self.bundle_bytes},
        )
        self.state_root = self.tmpdir / "state"
        self.install_root = self.tmpdir / "install"
        self.install_root.mkdir(parents=True)
        self.install_targets = [{"id": "default", "host_path": str(self.install_root)}]

    def tearDown(self) -> None:
        self.server.shutdown()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @mock.patch.dict(os.environ, {"TEST_DIST_KEY": TEST_API_KEY})
    def test_dry_run_returns_entries_without_install(self) -> None:
        config = _make_distributor_config(self.base_url, self.pub_str)
        entries, manifest_entry = sync_distributor_set(
            config, _make_source(), self.state_root, self.install_targets,
            dry_run=True,
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "deploy")
        self.assertIsNotNone(manifest_entry)
        self.assertFalse((self.install_root / "deploy").exists())


if __name__ == "__main__":
    unittest.main()
