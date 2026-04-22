"""Reusable test fixture: local HTTP server serving signed distributor manifests and bundles.

Usage::

    fixture = start_test_distributor_server(tmp_path)
    try:
        # fixture.url, fixture.api_key, fixture.public_key_str, ...
    finally:
        fixture.shutdown()
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from runtime_manager.distribution.bundle import pack_skill_bundle
from runtime_manager.distribution.signing import public_key_to_config_str, sign_manifest

TEST_API_KEY = "e2e-test-key-42"
TEST_CLIENT_ID = "e2e-client"
TEST_DISTRIBUTOR_ID = "e2e-dist"
TEST_DIST_KEY_ENV = "E2E_DIST_KEY"


@dataclass
class SkillArtifacts:
    manifest_bytes: bytes
    bundle_bytes: bytes
    bundle_sha256: str
    skill_name: str
    version: int
    download_path: str


@dataclass
class DistributorFixture:
    server: http.server.HTTPServer
    url: str
    api_key: str
    public_key_str: str
    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey
    artifacts: list[SkillArtifacts]

    def shutdown(self) -> None:
        self.server.shutdown()


def build_test_skill_artifacts(
    tmp_path: Path,
    private_key: Ed25519PrivateKey,
    *,
    skills: list[dict[str, Any]] | None = None,
    manifest_version: int = 14,
    distributor_id: str = TEST_DISTRIBUTOR_ID,
    client_id: str = TEST_CLIENT_ID,
) -> tuple[bytes, list[SkillArtifacts]]:
    """Build signed manifest + bundle(s) for test skills.

    Returns ``(manifest_bytes, [SkillArtifacts, ...])``.
    """
    if skills is None:
        skills = [{"name": "deploy", "version": 8, "targets": ["box"]}]

    all_artifacts: list[SkillArtifacts] = []
    manifest_skills: list[dict[str, Any]] = []

    for skill_def in skills:
        name = skill_def["name"]
        version = skill_def.get("version", 1)
        targets = skill_def.get("targets", ["box"])

        skill_dir = tmp_path / "src" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"# {name}\n\nA test skill.\n", encoding="utf-8",
        )
        refs = skill_dir / "references"
        refs.mkdir(exist_ok=True)
        (refs / "guide.md").write_text(
            "## Guide\n\nUsage info.\n", encoding="utf-8",
        )

        bundle_out = tmp_path / "bundles"
        bundle_out.mkdir(parents=True, exist_ok=True)
        bundle_path = pack_skill_bundle(
            skill_dir, version, name=name, output_dir=bundle_out,
        )
        bundle_bytes = bundle_path.read_bytes()
        bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()
        download_path = f"/skills/{name}/{version}/bundle.tar.gz"

        entry: dict[str, Any] = {
            "name": name,
            "version": version,
            "sha256": bundle_sha,
            "size_bytes": len(bundle_bytes),
            "download_url": download_path,
            "targets": targets,
        }
        for optional_key in ("min_version", "min_version_reason", "changelog"):
            if optional_key in skill_def:
                entry[optional_key] = skill_def[optional_key]

        manifest_skills.append(entry)
        all_artifacts.append(SkillArtifacts(
            manifest_bytes=b"",
            bundle_bytes=bundle_bytes,
            bundle_sha256=bundle_sha,
            skill_name=name,
            version=version,
            download_path=download_path,
        ))

    manifest_dict: dict[str, Any] = {
        "schema_version": 1,
        "distributor_id": distributor_id,
        "client_id": client_id,
        "manifest_version": manifest_version,
        "updated_at": "2026-04-21T10:00:00Z",
        "skills": manifest_skills,
    }
    signed = sign_manifest(manifest_dict, private_key)
    manifest_bytes = json.dumps(signed, indent=2).encode("utf-8")

    return manifest_bytes, all_artifacts


class _DistributorHandler(http.server.BaseHTTPRequestHandler):

    def _check_auth(self) -> bool:
        expected = getattr(self.server, "api_key", "")
        if not expected:
            return True
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {expected}":
            self.send_error(401, "Unauthorized")
            return False
        return True

    def do_HEAD(self) -> None:
        if not self._check_auth():
            return
        if self.path == "/manifest":
            body = getattr(self.server, "manifest_bytes", b"")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return
        self.send_error(404)

    def do_GET(self) -> None:
        if not self._check_auth():
            return

        if self.path == "/manifest":
            body = getattr(self.server, "manifest_bytes", b"")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        bundles: dict[str, bytes] = getattr(self.server, "bundles", {})
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


def start_test_distributor_server(
    tmp_path: Path,
    *,
    private_key: Ed25519PrivateKey | None = None,
    skills: list[dict[str, Any]] | None = None,
    manifest_version: int = 14,
    api_key: str = TEST_API_KEY,
    distributor_id: str = TEST_DISTRIBUTOR_ID,
    client_id: str = TEST_CLIENT_ID,
) -> DistributorFixture:
    """Start a local HTTP server serving signed test artifacts.

    Returns a :class:`DistributorFixture` — call ``fixture.shutdown()`` when done.
    """
    if private_key is None:
        private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    public_key_str = public_key_to_config_str(public_key)

    manifest_bytes, artifacts = build_test_skill_artifacts(
        tmp_path,
        private_key,
        skills=skills,
        manifest_version=manifest_version,
        distributor_id=distributor_id,
        client_id=client_id,
    )

    bundles: dict[str, bytes] = {}
    for art in artifacts:
        bundles[art.download_path] = art.bundle_bytes

    server = http.server.HTTPServer(("127.0.0.1", 0), _DistributorHandler)
    server.manifest_bytes = manifest_bytes  # type: ignore[attr-defined]
    server.bundles = bundles  # type: ignore[attr-defined]
    server.api_key = api_key  # type: ignore[attr-defined]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    host, port = server.server_address
    url = f"http://{host}:{port}"

    return DistributorFixture(
        server=server,
        url=url,
        api_key=api_key,
        public_key_str=public_key_str,
        private_key=private_key,
        public_key=public_key,
        artifacts=artifacts,
    )
