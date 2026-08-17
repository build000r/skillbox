"""Contract tests for the pinned, authenticated DCG distribution.

Every network-shaped test runs against fixtures or an injected fetcher; nothing
here performs a live download. The committed pin is re-proved offline against
the real upstream ``SHA256SUMS`` + ``.minisig`` under
``tests/fixtures/dcg_distribution/release-metadata/``.
"""
from __future__ import annotations

import base64
import hashlib
import io
import lzma
import os
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_MANAGER_DIR = ROOT_DIR / ".env-manager"
if str(ENV_MANAGER_DIR) not in sys.path:
    sys.path.insert(0, str(ENV_MANAGER_DIR))

from runtime_manager import dcg_distribution as dist  # noqa: E402

FIXTURE_DIR = ROOT_DIR / "tests" / "fixtures" / "dcg_distribution"
METADATA_DIR = FIXTURE_DIR / "release-metadata"
ASSERT_INSTALLED = FIXTURE_DIR / "assert-installed.sh"

LINUX_X86 = "dcg-x86_64-unknown-linux-musl.tar.xz"

# Snapshot of the real, unpatched pin. Subprocess fixtures (assert-installed.sh)
# always see these values, never a test's synthetic digest.
REAL_PINNED_SHA256 = dict(dist.PINNED_ASSET_SHA256)


# ---------------------------------------------------------------------------
# Synthetic release builder (no network, no committed multi-MB binaries)
# ---------------------------------------------------------------------------


def _fake_dcg_script(version: str = "0.6.7") -> bytes:
    """A tiny stand-in binary that answers --version and mcp-server."""
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import json, sys
        argv = sys.argv[1:]
        if argv[:1] == ["--version"]:
            print("{version}")
            raise SystemExit(0)
        if argv[:1] == ["mcp-server"]:
            line = sys.stdin.readline()
            request = json.loads(line)
            print(json.dumps({{
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {{
                    "protocolVersion": "2024-11-05",
                    "capabilities": {{"tools": {{"listChanged": False}}}},
                    "serverInfo": {{"name": "dcg", "version": "{version}"}},
                }},
            }}))
            raise SystemExit(0)
        print("error: unrecognized subcommand", file=sys.stderr)
        raise SystemExit(2)
        """
    ).encode("utf-8")


def _build_tar_xz(members: dict[str, bytes], *, mode: int = 0o755) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = mode
            archive.addfile(info, io.BytesIO(payload))
    return lzma.compress(buffer.getvalue())


def _minisign_keypair(seed: bytes = b"skillbox-dcg-distribution-test--32") -> tuple[str, Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(seed).digest())
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw_public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key_id = hashlib.sha256(raw_public).digest()[:8]
    config = base64.b64encode(b"Ed" + key_id + raw_public).decode("ascii")
    return config, private, key_id


def _sign_minisig(
    payload: bytes,
    private: Ed25519PrivateKey,
    key_id: bytes,
    trusted_comment: str,
    *,
    algorithm: bytes = b"ED",
) -> str:
    message = hashlib.blake2b(payload, digest_size=64).digest() if algorithm == b"ED" else payload
    signature = private.sign(message)
    blob = base64.b64encode(algorithm + key_id + signature).decode("ascii")
    global_signature = base64.b64encode(
        private.sign(signature + trusted_comment.encode("utf-8"))
    ).decode("ascii")
    return (
        "untrusted comment: signature from minisign secret key\n"
        f"{blob}\n"
        f"trusted comment: {trusted_comment}\n"
        f"{global_signature}\n"
    )


class FakeRelease:
    """One synthetic, correctly-signed release for the pinned asset name."""

    def __init__(self, asset: str = LINUX_X86, version: str = "0.6.7") -> None:
        self.asset = asset
        self.payload = _build_tar_xz({f"dcg-{version}/{dist.BINARY_NAME}": _fake_dcg_script(version)})
        self.sha256 = hashlib.sha256(self.payload).hexdigest()
        self.public_key, self._private, self._key_id = _minisign_keypair()
        self.minisig = _sign_minisig(
            self.payload,
            self._private,
            self._key_id,
            dist.expected_trusted_comment(asset),
        )
        self.fetch_log: list[str] = []

    def sign(self, payload: bytes, trusted_comment: str) -> str:
        return _sign_minisig(payload, self._private, self._key_id, trusted_comment)

    def fetch(self, url: str) -> bytes:
        self.fetch_log.append(url)
        if url.endswith(".minisig"):
            return self.minisig.encode("utf-8")
        if url.endswith(self.asset):
            return self.payload
        raise AssertionError(f"unexpected fetch: {url}")

    def offline_fetch(self, url: str) -> bytes:
        raise AssertionError(f"network used while offline: {url}")

    def patches(self):
        return (
            mock.patch.dict(dist.PINNED_ASSET_SHA256, {self.asset: self.sha256}, clear=False),
            mock.patch.object(dist, "DCG_MINISIGN_PUBLIC_KEY", self.public_key),
        )


class _PatchedRelease:
    """Context manager applying a FakeRelease's module patches."""

    def __init__(self, release: FakeRelease) -> None:
        self.release = release
        self._patches = release.patches()

    def __enter__(self) -> FakeRelease:
        for patch in self._patches:
            patch.start()
        return self.release

    def __exit__(self, *exc_info) -> None:
        for patch in reversed(self._patches):
            patch.stop()


# ---------------------------------------------------------------------------
# PC-DIST-1: one version / provenance source
# ---------------------------------------------------------------------------


class PinProvenanceTest(unittest.TestCase):
    def test_pin_is_v067_from_the_approved_release(self) -> None:
        self.assertEqual(dist.DCG_VERSION, "v0.6.7")
        self.assertEqual(
            dist.DCG_RELEASE_TAG_URL,
            "https://github.com/Dicklesworthstone/destructive_command_guard/"
            "releases/tag/v0.6.7",
        )
        self.assertTrue(dist.DCG_RELEASE_DOWNLOAD_BASE.startswith("https://"))

    def test_pinned_digests_match_the_upstream_sha256sums(self) -> None:
        rows = dist.parse_sha256sums((METADATA_DIR / "SHA256SUMS").read_text(encoding="utf-8"))
        self.assertEqual(len(dist.PINNED_ASSET_SHA256), 4)
        for asset, sha256 in dist.PINNED_ASSET_SHA256.items():
            with self.subTest(asset=asset):
                self.assertIn(asset, rows, f"{asset} missing from upstream SHA256SUMS")
                self.assertEqual(sha256, rows[asset])

    def test_upstream_sha256sums_verifies_against_the_pinned_minisign_key(self) -> None:
        """The digest table's authenticity, re-proved offline from fixtures."""
        parsed = dist.verify_minisign(
            (METADATA_DIR / "SHA256SUMS").read_bytes(),
            (METADATA_DIR / "SHA256SUMS.minisig").read_text(encoding="utf-8"),
            asset="SHA256SUMS",
            require_trusted_comment=False,
        )
        self.assertEqual(parsed.key_id, dist.DCG_MINISIGN_KEY_ID)
        self.assertIn(dist.DCG_VERSION, parsed.trusted_comment)
        self.assertIn(dist.DCG_RELEASE_SOURCE_COMMIT, parsed.trusted_comment)

    def test_every_supported_asset_has_a_committed_upstream_minisig(self) -> None:
        for asset in dist.supported_assets():
            with self.subTest(asset=asset):
                path = METADATA_DIR / f"{asset}.minisig"
                self.assertTrue(path.is_file(), f"missing fixture {path}")
                parsed = dist.parse_minisig(path.read_text(encoding="utf-8"))
                self.assertEqual(parsed.key_id, dist.DCG_MINISIGN_KEY_ID)
                self.assertEqual(
                    parsed.trusted_comment.strip(), dist.expected_trusted_comment(asset)
                )

    def test_cache_key_is_version_asset_digest(self) -> None:
        self.assertEqual(
            dist.cache_key(LINUX_X86),
            f"dcg/v0.6.7/{LINUX_X86}/{dist.PINNED_ASSET_SHA256[LINUX_X86]}",
        )

    def test_describe_pin_is_machine_readable(self) -> None:
        payload = dist.describe_pin("linux", "x86_64")
        self.assertEqual(payload["id"], "dcg-bin")
        self.assertEqual(payload["version"], "v0.6.7")
        self.assertEqual(payload["asset"], LINUX_X86)
        self.assertEqual(payload["minisign_key_id"], dist.DCG_MINISIGN_KEY_ID)
        self.assertEqual(payload["mcp_command"], "mcp-server")


# ---------------------------------------------------------------------------
# PC-DIST-2: strict supported-platform mapping
# ---------------------------------------------------------------------------


class PlatformMappingTest(unittest.TestCase):
    CASES = {
        ("Darwin", "arm64"): "dcg-aarch64-apple-darwin.tar.xz",
        ("Darwin", "aarch64"): "dcg-aarch64-apple-darwin.tar.xz",
        ("Darwin", "x86_64"): "dcg-x86_64-apple-darwin.tar.xz",
        ("darwin", "amd64"): "dcg-x86_64-apple-darwin.tar.xz",
        ("Linux", "aarch64"): "dcg-aarch64-unknown-linux-gnu.tar.xz",
        ("Linux", "arm64"): "dcg-aarch64-unknown-linux-gnu.tar.xz",
        ("Linux", "x86_64"): LINUX_X86,
        ("Linux", "AMD64"): LINUX_X86,
        ("Linux", "x86-64"): LINUX_X86,
    }

    def test_every_supported_tuple_maps_to_its_pinned_asset(self) -> None:
        for (system, machine), asset in self.CASES.items():
            with self.subTest(system=system, machine=machine):
                self.assertEqual(dist.resolve_asset(system, machine), asset)

    def test_wsl_uses_the_linux_mapping(self) -> None:
        # WSL's kernel reports Linux, so no special case is needed or wanted.
        self.assertEqual(dist.resolve_asset("Linux", "x86_64"), LINUX_X86)

    def test_native_windows_is_unsupported_and_fails_closed(self) -> None:
        for system in ("Windows", "win32", "MSYS"):
            with self.subTest(system=system):
                with self.assertRaises(dist.UnsupportedPlatformError) as ctx:
                    dist.resolve_asset(system, "x86_64")
                self.assertEqual(ctx.exception.code, "DCG_UNSUPPORTED_PLATFORM")
                self.assertTrue(ctx.exception.next_actions)

    def test_unknown_architecture_fails_closed_with_remediation(self) -> None:
        with self.assertRaises(dist.UnsupportedPlatformError) as ctx:
            dist.resolve_asset("Linux", "riscv64")
        payload = ctx.exception.to_payload()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "DCG_UNSUPPORTED_PLATFORM")
        self.assertIn("linux/x86_64", payload["error"]["context"]["supported_platforms"])

    def test_unlisted_asset_name_is_rejected(self) -> None:
        with self.assertRaises(dist.UnsupportedPlatformError):
            dist.cache_key("dcg-x86_64-pc-windows-msvc.zip")

    def test_no_windows_asset_is_pinned(self) -> None:
        self.assertFalse([a for a in dist.supported_assets() if "windows" in a])


# ---------------------------------------------------------------------------
# Digest + signature verification
# ---------------------------------------------------------------------------


class VerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.release = FakeRelease()
        self.patched = _PatchedRelease(self.release)
        self.patched.__enter__()
        self.addCleanup(self.patched.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_root = Path(self.tmp.name) / "cache"

    def test_digest_success(self) -> None:
        pin = dist.asset_pin("linux", "x86_64")
        self.assertEqual(dist.verify_digest(self.release.payload, pin), self.release.sha256)

    def test_digest_mismatch_fails_closed(self) -> None:
        pin = dist.asset_pin("linux", "x86_64")
        with self.assertRaises(dist.DigestMismatchError) as ctx:
            dist.verify_digest(self.release.payload + b"x", pin)
        self.assertEqual(ctx.exception.code, "DCG_DIGEST_MISMATCH")
        self.assertEqual(ctx.exception.context["expected_sha256"], self.release.sha256)

    def test_signature_from_a_foreign_key_is_rejected(self) -> None:
        _, other_private, other_key_id = _minisign_keypair(b"attacker-key-material")
        forged = _sign_minisig(
            self.release.payload,
            other_private,
            other_key_id,
            dist.expected_trusted_comment(LINUX_X86),
        )
        with self.assertRaises(dist.SignatureError) as ctx:
            dist.verify_minisign(self.release.payload, forged, asset=LINUX_X86)
        self.assertEqual(ctx.exception.code, "DCG_SIGNATURE_INVALID")
        self.assertIn("key id", ctx.exception.message)

    def test_signature_over_other_bytes_is_rejected(self) -> None:
        with self.assertRaises(dist.SignatureError):
            dist.verify_minisign(self.release.payload + b"tamper", self.release.minisig, asset=LINUX_X86)

    def test_replayed_signature_for_another_asset_is_rejected(self) -> None:
        """A valid signature whose trusted comment names a different asset."""
        other = "dcg-aarch64-apple-darwin.tar.xz"
        replayed = self.release.sign(self.release.payload, dist.expected_trusted_comment(other))
        with self.assertRaises(dist.SignatureError) as ctx:
            dist.verify_minisign(self.release.payload, replayed, asset=LINUX_X86)
        self.assertIn("trusted comment", ctx.exception.message)

    def test_tampered_trusted_comment_is_rejected(self) -> None:
        lines = self.release.minisig.splitlines()
        lines[2] = f"trusted comment: {dist.expected_trusted_comment(LINUX_X86)} EXTRA"
        with self.assertRaises(dist.SignatureError) as ctx:
            dist.verify_minisign(self.release.payload, "\n".join(lines), asset=LINUX_X86)
        self.assertIn("global signature", ctx.exception.message)

    def test_malformed_minisig_is_rejected(self) -> None:
        for body in ("", "untrusted comment: x\n", "a\nnot-base64!!\nb\nc\n"):
            with self.subTest(body=body):
                with self.assertRaises(dist.SignatureError):
                    dist.verify_minisign(self.release.payload, body, asset=LINUX_X86)

    def test_missing_metadata_fails_closed(self) -> None:
        def fetch(url: str) -> bytes:
            if url.endswith(".minisig"):
                raise OSError("404 Not Found")
            return self.release.payload

        with self.assertRaises(dist.MetadataMissingError) as ctx:
            dist.resolve_verified_payload(cache_root=self.cache_root, system="linux", machine="x86_64", fetch=fetch, env={})
        self.assertEqual(ctx.exception.code, "DCG_METADATA_UNREADABLE")
        self.assertFalse(self.cache_root.exists(), "unverified asset must not be cached")


# ---------------------------------------------------------------------------
# PC-DIST-3: cache / offline / upgrade
# ---------------------------------------------------------------------------


class CacheAndOfflineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.release = FakeRelease()
        self.patched = _PatchedRelease(self.release)
        self.patched.__enter__()
        self.addCleanup(self.patched.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_root = Path(self.tmp.name) / "cache"

    def _resolve(self, **kwargs):
        params = {
            "cache_root": self.cache_root,
            "system": "linux",
            "machine": "x86_64",
            "fetch": self.release.fetch,
            "env": {},
        }
        params.update(kwargs)
        return dist.resolve_verified_payload(**params)

    def test_cache_miss_downloads_then_cache_hit_does_not(self) -> None:
        first = self._resolve()
        self.assertEqual(first.source, "download")
        self.assertEqual(len(self.release.fetch_log), 2)

        second = self._resolve(fetch=self.release.offline_fetch)
        self.assertEqual(second.source, "cache")
        self.assertEqual(second.payload, first.payload)

    def test_cache_layout_uses_the_contract_cache_key(self) -> None:
        resolution = self._resolve()
        expected = self.cache_root / resolution.pin.cache_key
        self.assertTrue((expected / LINUX_X86).is_file())
        self.assertTrue((expected / f"{LINUX_X86}.minisig").is_file())

    def test_corrupted_cached_byte_fails_closed(self) -> None:
        resolution = self._resolve()
        cached = self.cache_root / resolution.pin.cache_key / LINUX_X86
        raw = bytearray(cached.read_bytes())
        raw[0] ^= 0xFF
        cached.write_bytes(bytes(raw))
        with self.assertRaises(dist.DigestMismatchError):
            self._resolve(fetch=self.release.offline_fetch)

    def test_offline_with_empty_cache_fails_closed(self) -> None:
        with self.assertRaises(dist.OfflineCacheMissError) as ctx:
            self._resolve(allow_network=False, fetch=self.release.offline_fetch)
        self.assertEqual(ctx.exception.code, "DCG_OFFLINE_CACHE_MISS")
        self.assertTrue(ctx.exception.next_actions)

    def test_offline_with_warm_cache_succeeds(self) -> None:
        self._resolve()
        resolution = self._resolve(allow_network=False, fetch=self.release.offline_fetch)
        self.assertEqual(resolution.source, "cache")

    def test_default_cache_root_is_on_the_persistent_mount(self) -> None:
        root = dist.default_cache_root({"SKILLBOX_HOME_ROOT": "/home/sandbox"})
        self.assertEqual(root, Path("/home/sandbox/.local/share/skillbox/dcg"))
        # /home/sandbox/.local is a persistent bind mount in docker-compose.yml,
        # so the verified cache survives container replacement.
        compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("/home/sandbox/.local", compose)


class EnvOverrideTest(unittest.TestCase):
    def test_matching_declaration_is_accepted(self) -> None:
        pin = dist.asset_pin("linux", "x86_64")
        resolved = dist.validate_env_overrides(
            {
                dist.PIN_URL_ENV: pin.url,
                dist.PIN_SHA256_ENV: pin.sha256,
            },
            system="linux",
            machine="x86_64",
        )
        self.assertEqual(resolved.asset, pin.asset)

    def test_empty_declaration_falls_back_to_the_pin(self) -> None:
        resolved = dist.validate_env_overrides(
            {dist.PIN_URL_ENV: "", dist.PIN_SHA256_ENV: ""},
            system="linux",
            machine="x86_64",
        )
        self.assertEqual(resolved.version, "v0.6.7")

    def test_developer_opt_out_url_is_rejected(self) -> None:
        with self.assertRaises(dist.PinOverrideError) as ctx:
            dist.validate_env_overrides(
                {dist.PIN_URL_ENV: "https://example.invalid/dcg.tar.xz"},
                system="linux",
                machine="x86_64",
            )
        self.assertEqual(ctx.exception.code, "DCG_PIN_OVERRIDE_REJECTED")

    def test_developer_opt_out_digest_is_rejected(self) -> None:
        with self.assertRaises(dist.PinOverrideError):
            dist.validate_env_overrides(
                {dist.PIN_SHA256_ENV: "0" * 64}, system="linux", machine="x86_64"
            )


# ---------------------------------------------------------------------------
# Archive safety
# ---------------------------------------------------------------------------


class ArchiveTest(unittest.TestCase):
    def test_extracts_the_single_dcg_member(self) -> None:
        payload = _build_tar_xz(
            {"dcg-0.6.7/README.md": b"docs", "dcg-0.6.7/dcg": _fake_dcg_script()}
        )
        self.assertEqual(dist.extract_dcg_binary(payload, asset=LINUX_X86), _fake_dcg_script())

    def test_path_traversal_member_is_rejected(self) -> None:
        payload = _build_tar_xz({"../../etc/dcg": b"evil"})
        with self.assertRaises(dist.ArchiveError) as ctx:
            dist.extract_dcg_binary(payload, asset=LINUX_X86)
        self.assertEqual(ctx.exception.code, "DCG_ARCHIVE_INVALID")

    def test_absolute_member_is_rejected(self) -> None:
        payload = _build_tar_xz({"/etc/dcg": b"evil"})
        with self.assertRaises(dist.ArchiveError):
            dist.extract_dcg_binary(payload, asset=LINUX_X86)

    def test_missing_or_duplicate_dcg_member_is_rejected(self) -> None:
        for members in ({"dcg-0.6.7/other": b"x"}, {"a/dcg": b"x", "b/dcg": b"y"}):
            with self.subTest(members=sorted(members)):
                with self.assertRaises(dist.ArchiveError):
                    dist.extract_dcg_binary(_build_tar_xz(members), asset=LINUX_X86)

    def test_non_xz_payload_is_rejected(self) -> None:
        with self.assertRaises(dist.ArchiveError):
            dist.extract_dcg_binary(b"not an archive", asset=LINUX_X86)


# ---------------------------------------------------------------------------
# Install / upgrade / provenance
# ---------------------------------------------------------------------------


class InstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.release = FakeRelease()
        self.patched = _PatchedRelease(self.release)
        self.patched.__enter__()
        self.addCleanup(self.patched.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_root = Path(self.tmp.name) / "state"
        self.cache_root = self.state_root / "home" / ".local" / "share" / "skillbox" / "dcg"
        self.target = self.state_root / "home" / ".local" / "bin" / "dcg"

    def _install(self, **kwargs):
        params = {
            "cache_root": self.cache_root,
            "system": "linux",
            "machine": "x86_64",
            "fetch": self.release.fetch,
            "env": {},
        }
        params.update(kwargs)
        return dist.install_verified_binary(self.target, **params)

    def test_install_writes_an_executable_and_reports_provenance(self) -> None:
        record = self._install()
        self.assertEqual(record["id"], "dcg-bin")
        self.assertEqual(record["action"], "install")
        self.assertEqual(record["state"], "missing")
        self.assertEqual(record["version"], "v0.6.7")
        self.assertTrue(record["verified"])
        self.assertEqual(record["asset"], LINUX_X86)
        self.assertEqual(record["sha256"], self.release.sha256)
        self.assertEqual(record["minisign_key_id"], dist.DCG_MINISIGN_KEY_ID)
        self.assertEqual(record["mcp_command"], "mcp-server")
        self.assertTrue(self.target.is_file())
        self.assertTrue(os.access(self.target, os.X_OK))

    def test_rerun_is_a_no_op(self) -> None:
        self._install()
        record = self._install(fetch=self.release.offline_fetch)
        self.assertEqual(record["action"], "exists")
        self.assertEqual(record["state"], "ok")
        self.assertEqual(record["source"], "installed")

    def test_stale_version_is_upgraded_in_place(self) -> None:
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_bytes(_fake_dcg_script("0.6.5"))
        self.target.chmod(0o755)
        record = self._install()
        self.assertEqual(record["state"], "stale")
        self.assertEqual(record["action"], "reinstall")
        self.assertEqual(dist.installed_version(self.target), "v0.6.7")

    def test_dry_run_mutates_nothing(self) -> None:
        record = self._install(fetch=self.release.offline_fetch, dry_run=True)
        self.assertTrue(record["dry_run"])
        self.assertEqual(record["action"], "install")
        self.assertFalse(self.target.exists())

    def test_install_survives_container_replacement(self) -> None:
        """Both the binary and the verified cache live on the persistent mount."""
        self._install()
        persistent = self.state_root / "home" / ".local"
        self.assertTrue(self.target.is_relative_to(persistent))
        self.assertTrue(self.cache_root.is_relative_to(persistent))
        # Simulate a replaced container: only the persistent mount survives.
        for stray in self.state_root.iterdir():
            self.assertEqual(stray.name, "home")
        record = self._install(fetch=self.release.offline_fetch, allow_network=False)
        self.assertEqual(record["action"], "exists")

    def test_install_fails_closed_on_unsupported_platform(self) -> None:
        with self.assertRaises(dist.UnsupportedPlatformError):
            self._install(system="windows", machine="x86_64")
        self.assertFalse(self.target.exists())

    def test_install_fails_closed_when_digest_drifts(self) -> None:
        def poisoned(url: str) -> bytes:
            if url.endswith(".minisig"):
                return self.release.minisig.encode("utf-8")
            return self.release.payload + b"poison"

        with self.assertRaises(dist.DigestMismatchError):
            self._install(fetch=poisoned)
        self.assertFalse(self.target.exists())

    def test_provenance_record_rejects_a_wrong_version_binary(self) -> None:
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_bytes(_fake_dcg_script("0.6.5"))
        self.target.chmod(0o755)
        with self.assertRaises(dist.InstalledVersionError) as ctx:
            dist.provenance_record(self.target, system="linux", machine="x86_64")
        self.assertEqual(ctx.exception.code, "DCG_VERSION_MISMATCH")

    def test_provenance_record_rejects_a_missing_binary(self) -> None:
        with self.assertRaises(dist.InstalledVersionError) as ctx:
            dist.provenance_record(self.target, system="linux", machine="x86_64")
        self.assertEqual(ctx.exception.code, "DCG_BINARY_MISSING")

    def test_assert_installed_fixture_reports_ok(self) -> None:
        self._install()
        env = dict(os.environ)
        env["DCG_BIN"] = str(self.target)
        # The synthetic binary implements --version and mcp-server, but this
        # assertion is about the digest/provenance line, so skip the handshake.
        env["DCG_SKIP_MCP"] = "1"
        completed = subprocess.run(
            [str(ASSERT_INSTALLED)], capture_output=True, text=True, env=env, timeout=60
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("DCG_DISTRIBUTION_OK", completed.stdout)
        self.assertIn("version=v0.6.7", completed.stdout)
        # The fixture resolves the asset for the real host platform, so the
        # expectation must be host-derived to stay portable (Linux CI + Mac
        # self-test gate).
        host_asset = dist.resolve_asset()
        self.assertIn(f"asset={host_asset}", completed.stdout)
        # The subprocess reads the real, unpatched pin.
        self.assertIn(f"sha256={REAL_PINNED_SHA256[host_asset]}", completed.stdout)
        self.assertIn(f"minisign_key_id={dist.DCG_MINISIGN_KEY_ID}", completed.stdout)
        self.assertIn("mcp_command=mcp-server", completed.stdout)

    def test_assert_installed_fixture_fails_on_a_missing_binary(self) -> None:
        env = dict(os.environ)
        env["DCG_BIN"] = str(self.target)
        completed = subprocess.run(
            [str(ASSERT_INSTALLED)], capture_output=True, text=True, env=env, timeout=60
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("DCG_DISTRIBUTION_FAIL", completed.stderr)
        self.assertIn("remediation:", completed.stderr)


class VersionParsingTest(unittest.TestCase):
    def test_bare_and_banner_output_normalizes(self) -> None:
        self.assertEqual(dist.normalize_version("0.6.7"), "v0.6.7")
        self.assertEqual(dist.normalize_version("v0.6.7\n"), "v0.6.7")
        self.assertEqual(dist.normalize_version("0.6.7\n\n  dcg 0.6.7 banner"), "v0.6.7")
        self.assertEqual(dist.normalize_version("dcg 0.6.7"), "v0.6.7")

    def test_unparseable_output_fails_closed(self) -> None:
        with self.assertRaises(dist.InstalledVersionError):
            dist.normalize_version("no version here")


# ---------------------------------------------------------------------------
# MCP readiness: mcp-server alive, mcp gone
# ---------------------------------------------------------------------------


REAL_DCG = os.environ.get("SKILLBOX_TEST_DCG_BIN") or "/home/skillbox/.local/bin/dcg"


class McpReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = Path(self.tmp.name) / "dcg"
        self.fake.write_bytes(_fake_dcg_script())
        self.fake.chmod(0o755)

    def test_runtime_manifest_declares_mcp_server_not_mcp(self) -> None:
        manifest = (ROOT_DIR / "workspace" / "runtime.yaml").read_text(encoding="utf-8")
        self.assertIn("${SKILLBOX_DCG_BIN} mcp-server", manifest)
        self.assertNotIn("${SKILLBOX_DCG_BIN} mcp\n", manifest)
        self.assertNotIn("${SKILLBOX_DCG_BIN} mcp ", manifest)

    def test_mcp_command_helper_uses_the_current_subcommand(self) -> None:
        self.assertEqual(dist.mcp_command("/bin/dcg"), ["/bin/dcg", "mcp-server"])
        self.assertEqual(dist.DCG_MCP_COMMAND, "mcp-server")

    def test_bounded_handshake_against_the_fixture_binary(self) -> None:
        report = dist.probe_mcp_ready(self.fake, timeout=20)
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["command"], "mcp-server")
        self.assertEqual(report["server_name"], "dcg")

    def test_obsolete_mcp_subcommand_fails_the_contract(self) -> None:
        report = dist.probe_mcp_ready(self.fake, subcommand="mcp", timeout=20)
        self.assertFalse(report["ready"], report)

    def test_readiness_report_requires_both_halves(self) -> None:
        report = dist.mcp_readiness_report(self.fake, timeout=20)
        self.assertTrue(report["ready"], report)
        self.assertEqual(report["command"], "mcp-server")
        self.assertFalse(report["obsolete"]["ready"])

    @unittest.skipUnless(
        os.path.isfile(REAL_DCG) and os.access(REAL_DCG, os.X_OK),
        f"pinned dcg binary not installed at {REAL_DCG}",
    )
    def test_installed_binary_matches_the_pin_and_serves_mcp_server(self) -> None:
        self.assertEqual(dist.installed_version(REAL_DCG), dist.DCG_VERSION)
        report = dist.mcp_readiness_report(REAL_DCG, timeout=20)
        self.assertTrue(report["ready"], report)
        self.assertFalse(report["obsolete"]["ready"], report["obsolete"])


if __name__ == "__main__":
    unittest.main()
