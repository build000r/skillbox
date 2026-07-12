"""Tests for install.sh's checksummed release-asset download path and the
pinned-identity sigstore verification.

Covers skillbox-9b8z (verified curl fallback) and skillbox-kvd6 (sigstore
identity policy). Both exercise real bash functions extracted from install.sh
with curl/cosign/sha256 tools stubbed on PATH, so no network or real signing is
needed.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = ROOT_DIR / "install.sh"


def _extract_function(func_name: str) -> str:
    """Pull a single bash function body out of install.sh by brace matching."""
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    marker = f"{func_name}() {{"
    start = text.index(marker)
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    raise ValueError(f"Could not find end of {func_name}() in install.sh")


def _config_value(var_name: str) -> str:
    """Read a top-level `VAR="..."` assignment's rendered default from install.sh.

    We just re-source the assignment inside the harness rather than parse it in
    Python, but expose the raw line so tests can assert intent if needed.
    """
    for line in INSTALL_SCRIPT.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{var_name}="):
            return line
    raise ValueError(f"{var_name} not found in install.sh")


class HarnessMixin(unittest.TestCase):
    def _write_stub(self, path: Path, body: str) -> None:
        path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _run_bash(self, script: str, path_prepend: str | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        if path_prepend:
            env["PATH"] = f"{path_prepend}:{env['PATH']}"
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=env,
        )


class ReleaseAssetDownloadTests(HarnessMixin):
    """skillbox-9b8z: curl fallback fetches published tarball + SHA256SUMS and
    verifies the checksum by default (no user-supplied --sha256)."""

    def _harness(self, body: str) -> str:
        deps = "\n".join(
            _extract_function(name)
            for name in ("have_cmd", "sha256_file", "download_release_assets")
        )
        return f"""
set -u
warn() {{ printf 'WARN: %s\\n' "$*" >&2; }}
err()  {{ printf 'ERROR: %s\\n' "$*" >&2; }}
ok()   {{ printf 'OK: %s\\n' "$*"; }}
PROXY_ARGS=()
RELEASE_ASSET_VERIFIED=0
DEFAULT_GITHUB_REPO="build000r/skillbox"
{deps}

{body}
"""

    def _make_curl_stub(self, stub_dir: Path, tarball_bytes: bytes, sha_manifest: str,
                        include_bundle: bool = True) -> None:
        """A curl stub that serves fixture assets keyed on the requested URL.

        It parses `-o <dest>` and the trailing URL out of argv, then writes the
        matching fixture. Any URL it does not recognize exits non-zero (404).
        """
        fixtures = stub_dir / "fixtures"
        fixtures.mkdir(parents=True, exist_ok=True)
        (fixtures / "tarball").write_bytes(tarball_bytes)
        (fixtures / "sums").write_text(sha_manifest, encoding="utf-8")
        (fixtures / "bundle").write_text('{"stub":"sigstore"}\n', encoding="utf-8")
        bundle_clause = (
            '  *.tar.gz.sigstore.json) cp "$FIX/bundle" "$dest"; exit 0 ;;\n'
            if include_bundle
            else '  *.tar.gz.sigstore.json) exit 22 ;;\n'
        )
        self._write_stub(
            stub_dir / "curl",
            f"""FIX="{fixtures}"
dest=""
url=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then dest="$arg"; fi
  case "$arg" in -*) : ;; *) url="$arg" ;; esac
  prev="$arg"
done
case "$url" in
  *SHA256SUMS) cp "$FIX/sums" "$dest"; exit 0 ;;
{bundle_clause}  *.tar.gz) cp "$FIX/tarball" "$dest"; exit 0 ;;
  *) exit 22 ;;
esac
""",
        )

    def test_release_asset_verifies_checksum_by_default(self) -> None:
        tarball = b"pretend skillbox source tarball\n"
        digest = hashlib.sha256(tarball).hexdigest()
        tag = "v9.9.9"
        manifest = f"{digest}  skillbox-{tag}.tar.gz\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stub_dir = tmp_path / "stubs"
            stub_dir.mkdir()
            self._make_curl_stub(stub_dir, tarball, manifest)
            dest = tmp_path / "work" / "skillbox.tar.gz"
            dest.parent.mkdir(parents=True)

            body = (
                f'download_release_assets "{dest}" "{tag}"\n'
                'echo "rc=$?"\n'
                'echo "verified=${RELEASE_ASSET_VERIFIED}"\n'
            )
            result = self._run_bash(self._harness(body), path_prepend=str(stub_dir))

            self.assertIn("rc=0", result.stdout, result.stderr)
            self.assertIn("verified=1", result.stdout, result.stderr)
            # Downloaded tarball and sigstore bundle landed beside dest.
            self.assertTrue(dest.is_file())
            self.assertTrue((tmp_path / "work" / "SHA256SUMS").is_file())
            self.assertTrue(Path(f"{dest}.sigstore.json").is_file())

    def test_release_asset_tampered_tarball_fails_verification(self) -> None:
        real = b"pretend skillbox source tarball\n"
        digest = hashlib.sha256(real).hexdigest()
        tampered = b"MALICIOUS payload injected\n"
        tag = "v9.9.9"
        manifest = f"{digest}  skillbox-{tag}.tar.gz\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stub_dir = tmp_path / "stubs"
            stub_dir.mkdir()
            # Serve the tampered tarball but the manifest for the real one.
            self._make_curl_stub(stub_dir, tampered, manifest)
            dest = tmp_path / "work" / "skillbox.tar.gz"
            dest.parent.mkdir(parents=True)

            body = (
                f'download_release_assets "{dest}" "{tag}"\n'
                'echo "rc=$?"\n'
                'echo "verified=${RELEASE_ASSET_VERIFIED}"\n'
            )
            result = self._run_bash(self._harness(body), path_prepend=str(stub_dir))

            self.assertNotIn("rc=0", result.stdout, result.stderr)
            self.assertIn("verified=0", result.stdout, result.stderr)
            self.assertIn("mismatch", (result.stdout + result.stderr).lower())

    def test_missing_release_asset_returns_nonzero_for_fallback(self) -> None:
        tag = "v9.9.9"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stub_dir = tmp_path / "stubs"
            stub_dir.mkdir()
            # curl stub that 404s on everything => no release asset exists.
            self._write_stub(stub_dir / "curl", "exit 22\n")
            dest = tmp_path / "work" / "skillbox.tar.gz"
            dest.parent.mkdir(parents=True)

            body = (
                f'download_release_assets "{dest}" "{tag}"\n'
                'echo "rc=$?"\n'
            )
            result = self._run_bash(self._harness(body), path_prepend=str(stub_dir))
            # Non-zero rc so download_source_tarball falls back to the archive.
            self.assertNotIn("rc=0", result.stdout, result.stderr)
            self.assertNotIn("verified=1", result.stdout)


class SigstorePinnedIdentityTests(HarnessMixin):
    """skillbox-kvd6: with cosign present, the bundle is verified against a
    pinned certificate identity/issuer; tampered/untrusted => install fails."""

    def _harness(self, body: str) -> str:
        deps = "\n".join(
            _extract_function(name)
            for name in ("have_cmd", "maybe_verify_sigstore_bundle")
        )
        issuer_line = _config_value("SIGSTORE_CERT_OIDC_ISSUER")
        identity_line = _config_value("SIGSTORE_CERT_IDENTITY_REGEXP")
        # These defaults reference DEFAULT_GITHUB_REPO via parameter expansion.
        return f"""
set -u
warn() {{ printf 'WARN: %s\\n' "$*" >&2; }}
err()  {{ printf 'ERROR: %s\\n' "$*" >&2; }}
ok()   {{ printf 'OK: %s\\n' "$*"; }}
DEFAULT_GITHUB_REPO="build000r/skillbox"
ALLOW_UNVERIFIED=0
{issuer_line}
{identity_line}
{deps}

{body}
"""

    def _cosign_stub(self, stub_dir: Path, *, succeed: bool) -> None:
        """cosign stub that asserts the pinned identity flags are passed and
        exits 0 (valid) or 1 (tampered/untrusted) accordingly."""
        rc = "0" if succeed else "1"
        self._write_stub(
            stub_dir / "cosign",
            f"""# Record argv for assertions.
printf '%s\\n' "$*" >>"{stub_dir}/cosign-args"
have_identity=0
have_issuer=0
have_bundle=0
for a in "$@"; do
  case "$a" in
    --certificate-identity-regexp) have_identity=1 ;;
    --certificate-oidc-issuer) have_issuer=1 ;;
    --bundle) have_bundle=1 ;;
  esac
done
if [ "$have_identity" != 1 ] || [ "$have_issuer" != 1 ] || [ "$have_bundle" != 1 ]; then
  echo "cosign stub: pinned identity/issuer/bundle flags missing" >&2
  exit 3
fi
exit {rc}
""",
        )

    def test_valid_bundle_invokes_pinned_identity_and_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stub_dir = tmp_path / "stubs"
            stub_dir.mkdir()
            self._cosign_stub(stub_dir, succeed=True)
            artifact = tmp_path / "skillbox.tar.gz"
            artifact.write_bytes(b"artifact\n")
            Path(f"{artifact}.sigstore.json").write_text('{"x":1}\n', encoding="utf-8")

            body = f'maybe_verify_sigstore_bundle "{artifact}"\necho "rc=$?"\n'
            result = self._run_bash(self._harness(body), path_prepend=str(stub_dir))

            self.assertIn("rc=0", result.stdout, result.stderr)
            args = (stub_dir / "cosign-args").read_text(encoding="utf-8")
            self.assertIn("verify-blob", args)
            self.assertIn("--certificate-identity-regexp", args)
            self.assertIn("--certificate-oidc-issuer", args)
            self.assertIn("token.actions.githubusercontent.com", args)
            self.assertIn("build000r/skillbox", args)

    def test_tampered_bundle_fails_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stub_dir = tmp_path / "stubs"
            stub_dir.mkdir()
            self._cosign_stub(stub_dir, succeed=False)
            artifact = tmp_path / "skillbox.tar.gz"
            artifact.write_bytes(b"artifact\n")
            Path(f"{artifact}.sigstore.json").write_text('{"tampered":1}\n', encoding="utf-8")

            body = f'maybe_verify_sigstore_bundle "{artifact}"\necho "rc=$?"\n'
            result = self._run_bash(self._harness(body), path_prepend=str(stub_dir))

            # Function should exit 1 (install abort); the trailing echo never runs.
            self.assertNotIn("rc=0", result.stdout)
            self.assertIn("verification failed", (result.stdout + result.stderr).lower())

    def test_tampered_bundle_bypassable_with_allow_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stub_dir = tmp_path / "stubs"
            stub_dir.mkdir()
            self._cosign_stub(stub_dir, succeed=False)
            artifact = tmp_path / "skillbox.tar.gz"
            artifact.write_bytes(b"artifact\n")
            Path(f"{artifact}.sigstore.json").write_text('{"tampered":1}\n', encoding="utf-8")

            body = (
                "ALLOW_UNVERIFIED=1\n"
                f'maybe_verify_sigstore_bundle "{artifact}"\necho "rc=$?"\n'
            )
            result = self._run_bash(self._harness(body), path_prepend=str(stub_dir))
            self.assertIn("rc=0", result.stdout, result.stderr)


if __name__ == "__main__":
    unittest.main()
