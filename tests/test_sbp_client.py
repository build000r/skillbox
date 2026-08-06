from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLIENT_PATH = ROOT / "scripts" / "lib" / "sbp_client.py"
SBP_PATH = ROOT / "scripts" / "sbp"
TAILNET_REMOTE = "http://100.100.0.10:8443"

SPEC = importlib.util.spec_from_file_location("sbp_client", CLIENT_PATH)
assert SPEC is not None and SPEC.loader is not None
SBP_CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SBP_CLIENT)


class Response:
    def __init__(
        self,
        body: bytes,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.body = body
        self._stream = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


class RedirectedResponse(Response):
    def __init__(self, body: bytes, final_url: str) -> None:
        super().__init__(body)
        self.final_url = final_url

    def geturl(self) -> str:
        return self.final_url


class ShortReadResponse(Response):
    def read(self, size: int = -1) -> bytes:
        return super().read(min(size, 4096))


class SbpClientUnitTests(unittest.TestCase):
    def _token(self, **overrides: object) -> tuple[str, dict[str, object]]:
        now = int(time.time())
        claims: dict[str, object] = {
            "aud": "sbpd",
            "email": "operator@example.invalid",
            "email_verified": True,
            "exp": now + 600,
            "iat": now,
            "iss": SBP_CLIENT.AMP_ISSUER,
            "jti": "lease-test",
            "project_id": "project-test",
            "sub": "project:project-test:user:user-test:thread:T-test",
            "thread_id": "T-test",
            "token_use": "exchanged",
            "user_id": "user-test",
        }
        claims.update(overrides)

        def encoded(value: object) -> str:
            raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        return f"{encoded({'alg': 'RS256', 'typ': 'JWT'})}.{encoded(claims)}.signature", claims

    @staticmethod
    def _minter(token: str):
        return lambda _audience, _ttl: token

    @staticmethod
    def _identity_headers(tree_sha: str) -> dict[str, str]:
        return {
            "X-Skill-Tree-Sha256": tree_sha,
            "X-SBP-Project-Id": "project-test",
            "X-SBP-Project-Alias": "build000r/skillbox",
            "X-SBP-Remote-Sha": "a" * 40,
            "X-SBP-Resolution-Receipt": "b" * 64,
            "X-SBP-Lease-Id": "lease-test",
            "X-SBP-Thread-Id": "T-test",
            "X-SBP-User-Id": "user-test",
            "X-SBP-Visibility": "private",
        }

    def test_search_encodes_query_and_prints_envelope_verbatim(self) -> None:
        body = b'{ "status": "ok", "result": [1] }\n'
        output = io.BytesIO()
        opener = mock.Mock(return_value=Response(body))

        result = SBP_CLIENT.run_remote_cass(
            "http://127.0.0.1:8443/",
            ["search", "exact phrase", "--json"],
            opener=opener,
            stdout=output,
        )

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), body)
        request = opener.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8443/v1/cass/search?q=exact+phrase",
        )
        self.assertEqual(opener.call_args.kwargs["timeout"], 90.0)

    def test_status_maps_to_read_only_endpoint(self) -> None:
        opener = mock.Mock(return_value=Response(b"{}"))

        result = SBP_CLIENT.run_remote_cass(
            "http://127.0.0.1:8443",
            ["--json", "status"],
            opener=opener,
            stdout=io.BytesIO(),
        )

        self.assertEqual(result, 0)
        self.assertEqual(
            opener.call_args.args[0].full_url,
            "http://127.0.0.1:8443/v1/cass/status",
        )

    def test_unsupported_remote_verb_fails_without_http_request(self) -> None:
        opener = mock.Mock()
        errors = io.StringIO()

        result = SBP_CLIENT.run_remote_cass(
            TAILNET_REMOTE,
            ["rebuild"],
            opener=opener,
            stdout=io.BytesIO(),
            stderr=errors,
        )

        self.assertEqual(result, 2)
        self.assertIn("does not support 'rebuild'", errors.getvalue())
        opener.assert_not_called()

    def test_remote_origin_rejects_unpinned_or_ambiguous_transports(self) -> None:
        invalid = (
            "ftp://100.100.0.10:8443",
            "http://8.8.8.8:8443",
            "http://user@example.invalid:8443",
            "http://100.100.0.10:8443/base",
            "http://100.100.0.10:8443?query=yes",
            "https://service.example.invalid",
        )
        for remote in invalid:
            with self.subTest(remote=remote):
                opener = mock.Mock()
                result = SBP_CLIENT.run_remote_cass(
                    remote,
                    ["status"],
                    opener=opener,
                    stdout=io.BytesIO(),
                    stderr=io.StringIO(),
                )
                self.assertEqual(result, 2)
                opener.assert_not_called()

    def test_authenticated_response_cannot_change_origin(self) -> None:
        token, _claims = self._token()
        errors = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            result = SBP_CLIENT.run_remote_cass(
                TAILNET_REMOTE,
                ["status"],
                opener=mock.Mock(
                    return_value=RedirectedResponse(
                        b"{}",
                        "http://100.100.0.11:8443/v1/cass/status",
                    )
                ),
                token_minter=self._minter(token),
                stdout=io.BytesIO(),
                stderr=errors,
            )
        self.assertEqual(result, 1)
        self.assertIn("changed transport origin", errors.getvalue())

    def test_remote_reads_reject_oversized_responses(self) -> None:
        errors = io.StringIO()
        cass_result = SBP_CLIENT.run_remote_cass(
            "http://127.0.0.1:8443",
            ["status"],
            opener=mock.Mock(
                return_value=ShortReadResponse(
                    b"x" * (SBP_CLIENT.MAX_CASS_RESPONSE_BYTES + 1)
                )
            ),
            stdout=io.BytesIO(),
            stderr=errors,
        )
        self.assertEqual(cass_result, 1)
        self.assertIn("maximum response size", errors.getvalue())

        skill_output = io.BytesIO()
        skill_result = SBP_CLIENT.run_remote_skill_pull(
            "http://127.0.0.1:8443",
            ["pull", "sample"],
            opener=mock.Mock(
                return_value=Response(b"x" * (SBP_CLIENT.MAX_SKILL_BUNDLE_BYTES + 1))
            ),
            stdout=skill_output,
        )
        self.assertEqual(skill_result, 1)
        self.assertEqual(
            json.loads(skill_output.getvalue())["error_code"],
            "SKILL_TRANSPORT_AUTH_UNAVAILABLE",
        )

    def _skill_bundle(self, root: Path, *, tamper_entry: bool = False) -> tuple[bytes, str]:
        source = root / "sample"
        source.mkdir()
        (source / "SKILL.md").write_text("# sample\n\nUse remote policy.\n", encoding="utf-8")
        SBP_CLIENT._bundle_api()
        from runtime_manager.distribution.bundle import pack_skill_bundle

        bundle_path = pack_skill_bundle(source, 1, name="sample", output_dir=root)
        if not tamper_entry:
            manifest = self._unpack_manifest(bundle_path, root / "verified")
            return bundle_path.read_bytes(), manifest.tree_sha256

        unpacked = root / "tampered"
        manifest = self._unpack_manifest(bundle_path, unpacked)
        (unpacked / "SKILL.md").write_text("# sample\n\nTampered.\n", encoding="utf-8")
        tampered_path = root / "tampered.tar.gz"
        with tarfile.open(tampered_path, "w:gz") as archive:
            for path in sorted(unpacked.rglob("*")):
                archive.add(path, arcname=path.relative_to(unpacked))
        return tampered_path.read_bytes(), manifest.tree_sha256

    def _unpack_manifest(self, bundle_path: Path, destination: Path) -> object:
        _, unpack_skill_bundle, _ = SBP_CLIENT._bundle_api()
        destination.mkdir()
        return unpack_skill_bundle(bundle_path, destination)

    def test_remote_skill_pull_verifies_bundle_and_prints_compatible_packet(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle, tree_sha = self._skill_bundle(Path(temp_dir))
        output = io.BytesIO()
        opener = mock.Mock(
            return_value=Response(bundle, {"X-Skill-Tree-Sha256": tree_sha})
        )

        result = SBP_CLIENT.run_remote_skill_pull(
            "http://127.0.0.1:8443/",
            ["pull", "sample", "--format", "json"],
            opener=opener,
            stdout=output,
        )

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(
            set(payload),
            {
                "ok",
                "schema_version",
                "name",
                "lifecycle",
                "entry_text",
                "tree_sha256",
                "entry_sha256",
                "receipt_sha256",
                "source_classification",
                "instructions",
            },
        )
        self.assertEqual(payload["schema_version"], "skill-pull-result/v1")
        self.assertEqual(payload["name"], "sample")
        self.assertEqual(payload["lifecycle"], "active")
        self.assertEqual(payload["entry_text"], "# sample\n\nUse remote policy.\n")
        self.assertEqual(payload["tree_sha256"], tree_sha)
        self.assertRegex(payload["entry_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(payload["receipt_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(payload["source_classification"], "host-canonical")
        self.assertEqual(
            payload["instructions"],
            "use this content immediately in the current session",
        )
        self.assertEqual(
            opener.call_args.args[0].full_url,
            "http://127.0.0.1:8443/v1/skill/pull/sample",
        )

    def test_remote_skill_pull_rejects_manifest_content_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle, tree_sha = self._skill_bundle(
                Path(temp_dir),
                tamper_entry=True,
            )
        output = io.BytesIO()

        result = SBP_CLIENT.run_remote_skill_pull(
            "http://127.0.0.1:8443",
            ["pull", "sample"],
            opener=mock.Mock(
                return_value=Response(bundle, {"X-Skill-Tree-Sha256": tree_sha})
            ),
            stdout=output,
        )

        self.assertEqual(result, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["error_code"], "SKILL_TREE_DRIFT")
        self.assertTrue(payload["retryable"])

    def test_nonloopback_cass_mints_a_fresh_short_lived_token_per_request(self) -> None:
        token, _claims = self._token()
        completed = subprocess.CompletedProcess([], 0, token + "\n", "")
        opener = mock.Mock(return_value=Response(b"{}"))
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(SBP_CLIENT.subprocess, "run", return_value=completed) as run,
        ):
            for _ in range(2):
                self.assertEqual(
                    SBP_CLIENT.run_remote_cass(
                        TAILNET_REMOTE,
                        ["status"],
                        opener=opener,
                        stdout=io.BytesIO(),
                    ),
                    0,
                )

        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(
                call.args[0],
                ["amp", "orb", "id-token", "--audience", "sbpd", "--ttl-seconds", "600"],
            )
            self.assertEqual(call.kwargs["timeout"], SBP_CLIENT.TOKEN_TIMEOUT_SECONDS)
        self.assertTrue(
            all(
                request.args[0].headers["Authorization"] == f"Bearer {token}"
                for request in opener.call_args_list
            )
        )

    def test_unauthorized_response_remints_once_and_retries_without_retaining_token(self) -> None:
        first_token, _ = self._token(jti="lease-first")
        refreshed_token, _ = self._token(jti="lease-refreshed")
        unauthorized = urllib.error.HTTPError(
            f"{TAILNET_REMOTE}/v1/cass/status",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"ok":false}'),
        )
        opener = mock.Mock(side_effect=(unauthorized, Response(b'{"ok":true}')))
        minter = mock.Mock(side_effect=(first_token, refreshed_token))
        with mock.patch.dict(os.environ, {}, clear=True):
            output = io.BytesIO()
            result = SBP_CLIENT.run_remote_cass(
                TAILNET_REMOTE,
                ["status"],
                opener=opener,
                token_minter=minter,
                stdout=output,
            )

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), b'{"ok":true}')
        self.assertEqual(minter.call_count, 2)
        self.assertEqual(minter.call_args_list, [mock.call("sbpd", "600")] * 2)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(
            opener.call_args_list[0].args[0].headers["Authorization"],
            f"Bearer {first_token}",
        )
        self.assertEqual(
            opener.call_args_list[1].args[0].headers["Authorization"],
            f"Bearer {refreshed_token}",
        )

    def test_loopback_unauthorized_response_mints_once_for_retry(self) -> None:
        token, _claims = self._token()
        remote = "http://127.0.0.1:8443"
        unauthorized = urllib.error.HTTPError(
            f"{remote}/v1/cass/status",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"ok":false}'),
        )
        opener = mock.Mock(side_effect=(unauthorized, Response(b'{"ok":true}')))
        minter = mock.Mock(return_value=token)

        with mock.patch.dict(os.environ, {}, clear=True):
            output = io.BytesIO()
            result = SBP_CLIENT.run_remote_cass(
                remote,
                ["status"],
                opener=opener,
                token_minter=minter,
                stdout=output,
            )

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), b'{"ok":true}')
        minter.assert_called_once_with("sbpd", "600")
        self.assertNotIn("Authorization", opener.call_args_list[0].args[0].headers)
        self.assertEqual(
            opener.call_args_list[1].args[0].headers["Authorization"],
            f"Bearer {token}",
        )

    def test_token_mint_timeout_malformed_output_and_env_shortcut_fail_closed(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                SBP_CLIENT.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["amp"], 10),
            ),
            self.assertRaisesRegex(ValueError, "unable to mint"),
        ):
            SBP_CLIENT._auth(TAILNET_REMOTE)

        for failure in (
            subprocess.CalledProcessError(1, ["amp", "orb", "id-token"]),
            subprocess.CompletedProcess([], 0, "not-a-jwt\n", ""),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(SBP_CLIENT.subprocess, "run", side_effect=None, return_value=failure)
                if isinstance(failure, subprocess.CompletedProcess)
                else mock.patch.object(SBP_CLIENT.subprocess, "run", side_effect=failure),
                self.assertRaisesRegex(ValueError, "unable to mint|parseable JWT"),
            ):
                SBP_CLIENT._auth(TAILNET_REMOTE)

        injected, _claims = self._token()
        with (
            mock.patch.dict(os.environ, {"SBP_TOKEN": injected}, clear=True),
            mock.patch.object(
                SBP_CLIENT.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["amp"], 10),
            ),
            self.assertRaisesRegex(ValueError, "unable to mint"),
        ):
            SBP_CLIENT._auth(TAILNET_REMOTE)

    def test_client_rejects_non_rs256_future_and_overlong_tokens(self) -> None:
        now = int(time.time())
        invalid = {
            "future": self._token(iat=now + 120, exp=now + 180)[0],
            "overlong": self._token(iat=now, exp=now + 3601)[0],
        }
        valid, claims = self._token()
        parts = valid.split(".")
        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
        invalid["wrong-algorithm"] = f"{header}.{parts[1]}.{parts[2]}"
        self.assertEqual(claims["token_use"], "exchanged")
        for name, token in invalid.items():
            with (
                self.subTest(name=name),
                mock.patch.dict(os.environ, {}, clear=True),
                self.assertRaisesRegex(ValueError, "identity contract"),
            ):
                SBP_CLIENT._auth(
                    TAILNET_REMOTE,
                    token_minter=self._minter(token),
                )

    def test_authenticated_pull_caches_exact_capsule_for_verified_offline_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            bundle, tree_sha = self._skill_bundle(source)
            token, _claims = self._token()
            environment = {
                "SBP_CACHE_DIR": str(root / "cache"),
                "SBP_PROJECT_ALIAS": "build000r/skillbox",
                "SBP_PROJECT_ID": "project-test",
                "SBP_RESUME_ID": "resume-test",
                "SBP_THREAD_ID": "T-test",
                "SBP_USER_ID": "user-test",
            }
            online_output = io.BytesIO()
            with mock.patch.dict(os.environ, environment, clear=True):
                result = SBP_CLIENT.run_remote_skill_pull(
                    TAILNET_REMOTE,
                    ["pull", "sample"],
                    opener=mock.Mock(
                        return_value=Response(bundle, self._identity_headers(tree_sha))
                    ),
                    token_minter=self._minter(token),
                    stdout=online_output,
                )
            self.assertEqual(result, 0, online_output.getvalue())
            online = json.loads(online_output.getvalue())
            self.assertEqual(online["transport_receipt"]["cache"], "private")
            cache_dir = next((root / "cache").iterdir())
            self.assertEqual(cache_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual((cache_dir / "capsule.tar.gz").stat().st_mode & 0o777, 0o600)
            self.assertEqual((cache_dir / "lock.json").stat().st_mode & 0o777, 0o600)
            lock = json.loads((cache_dir / "lock.json").read_text())
            self.assertEqual(
                {"project_alias", "project_id", "thread_id", "user_id", "resume_id", "lease_id"},
                {key for key in lock if key in {
                    "project_alias", "project_id", "thread_id", "user_id", "resume_id", "lease_id"
                }},
            )
            self.assertIsNone(lock["workspace_id"])
            self.assertEqual(
                lock["subject"],
                "project:project-test:user:user-test:thread:T-test",
            )

            offline_output = io.BytesIO()
            with mock.patch.dict(os.environ, {**environment, "SBP_OFFLINE": "1"}, clear=True):
                result = SBP_CLIENT.run_remote_skill_pull(
                    TAILNET_REMOTE,
                    ["pull", "sample"],
                    opener=mock.Mock(side_effect=AssertionError("offline read attempted network")),
                    stdout=offline_output,
                )
            self.assertEqual(result, 0, offline_output.getvalue())
            offline = json.loads(offline_output.getvalue())
            self.assertEqual(offline["entry_text"], online["entry_text"])
            self.assertEqual(offline["tree_sha256"], online["tree_sha256"])
            self.assertEqual(offline["transport_receipt"]["cache"], "private-offline")

            original_lock = (cache_dir / "lock.json").read_bytes()
            tampered_lock = json.loads(original_lock)
            tampered_lock.pop("lock_sha256")
            tampered_lock["subject"] = "project:other:user:user-test:thread:T-test"
            tampered_lock["lock_sha256"] = hashlib.sha256(
                SBP_CLIENT._canonical(tampered_lock)
            ).hexdigest()
            (cache_dir / "lock.json").write_bytes(SBP_CLIENT._canonical(tampered_lock))
            identity_output = io.BytesIO()
            with mock.patch.dict(os.environ, {**environment, "SBP_OFFLINE": "1"}, clear=True):
                result = SBP_CLIENT.run_remote_skill_pull(
                    TAILNET_REMOTE,
                    ["pull", "sample"],
                    opener=mock.Mock(side_effect=AssertionError("offline read attempted network")),
                    stdout=identity_output,
                )
            self.assertEqual(result, 1)
            self.assertEqual(
                json.loads(identity_output.getvalue())["error_code"],
                "SKILL_CACHE_UNAVAILABLE",
            )
            (cache_dir / "lock.json").write_bytes(original_lock)

            (cache_dir / "capsule.tar.gz").write_bytes(
                (cache_dir / "capsule.tar.gz").read_bytes() + b"tampered"
            )
            tampered_output = io.BytesIO()
            with mock.patch.dict(os.environ, {**environment, "SBP_OFFLINE": "1"}, clear=True):
                result = SBP_CLIENT.run_remote_skill_pull(
                    TAILNET_REMOTE,
                    ["pull", "sample"],
                    opener=mock.Mock(side_effect=AssertionError("offline read attempted network")),
                    stdout=tampered_output,
                )
            self.assertEqual(result, 1)
            self.assertEqual(
                json.loads(tampered_output.getvalue())["error_code"],
                "SKILL_CACHE_UNAVAILABLE",
            )

    def test_authenticated_cache_binds_optional_workspace_when_token_provides_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            bundle, tree_sha = self._skill_bundle(source)
            workspace = "workspace-test"
            subject = (
                "workspace:workspace-test:project:project-test:"
                "user:user-test:thread:T-test"
            )
            token, _claims = self._token(workspace_id=workspace, sub=subject)
            environment = {
                "SBP_CACHE_DIR": str(root / "cache"),
                "SBP_PROJECT_ALIAS": "build000r/skillbox",
                "SBP_PROJECT_ID": "project-test",
                "SBP_RESUME_ID": "resume-test",
                "SBP_THREAD_ID": "T-test",
                "SBP_USER_ID": "user-test",
                "SBP_WORKSPACE_ID": workspace,
            }
            with mock.patch.dict(os.environ, environment, clear=True):
                online_output = io.BytesIO()
                result = SBP_CLIENT.run_remote_skill_pull(
                    TAILNET_REMOTE,
                    ["pull", "sample"],
                    opener=mock.Mock(
                        return_value=Response(bundle, self._identity_headers(tree_sha))
                    ),
                    token_minter=self._minter(token),
                    stdout=online_output,
                )
            self.assertEqual(result, 0, online_output.getvalue())
            cache_dir = next((root / "cache").iterdir())
            lock = json.loads((cache_dir / "lock.json").read_text())
            self.assertEqual(lock["workspace_id"], workspace)
            self.assertEqual(lock["subject"], subject)

            with mock.patch.dict(
                os.environ,
                {**environment, "SBP_OFFLINE": "1"},
                clear=True,
            ):
                matching_output = io.BytesIO()
                matching = SBP_CLIENT.run_remote_skill_pull(
                    TAILNET_REMOTE,
                    ["pull", "sample"],
                    opener=mock.Mock(side_effect=AssertionError("offline read attempted network")),
                    stdout=matching_output,
                )
            self.assertEqual(matching, 0, matching_output.getvalue())

            mismatch_environment = {**environment, "SBP_OFFLINE": "1"}
            mismatch_environment.pop("SBP_WORKSPACE_ID")
            with mock.patch.dict(os.environ, mismatch_environment, clear=True):
                mismatch_output = io.BytesIO()
                mismatch = SBP_CLIENT.run_remote_skill_pull(
                    TAILNET_REMOTE,
                    ["pull", "sample"],
                    opener=mock.Mock(side_effect=AssertionError("offline read attempted network")),
                    stdout=mismatch_output,
                )
            self.assertEqual(mismatch, 1)
            self.assertEqual(
                json.loads(mismatch_output.getvalue())["error_code"],
                "SKILL_CACHE_UNAVAILABLE",
            )

    def test_offline_first_pull_and_tampered_cache_are_typed_failures(self) -> None:
        environment = {
            "SBP_OFFLINE": "1",
            "SBP_PROJECT_ALIAS": "build000r/skillbox",
            "SBP_PROJECT_ID": "project-test",
            "SBP_RESUME_ID": "resume-test",
            "SBP_THREAD_ID": "T-test",
            "SBP_USER_ID": "user-test",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            environment["SBP_CACHE_DIR"] = temp_dir
            output = io.BytesIO()
            with mock.patch.dict(os.environ, environment, clear=True):
                result = SBP_CLIENT.run_remote_skill_pull(
                    TAILNET_REMOTE,
                    ["pull", "sample"],
                    opener=mock.Mock(side_effect=AssertionError("offline read attempted network")),
                    stdout=output,
                )
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())["error_code"], "SKILL_CACHE_UNAVAILABLE")

    def test_skill_cache_refuses_agent_discovery_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            roots = (
                ROOT / ".agents" / "skills" / "cache",
                Path(temp_dir) / "unrelated-project" / ".agents" / "skills" / "cache",
                Path(temp_dir) / "unrelated-project" / ".claude" / "skills" / "cache",
                Path(temp_dir) / "unrelated-project" / ".codex" / "skills" / "cache",
            )
            for cache_root in roots:
                with self.subTest(cache_root=cache_root):
                    output = io.BytesIO()
                    environment = {"SBP_CACHE_DIR": str(cache_root), "SBP_OFFLINE": "1"}
                    with mock.patch.dict(os.environ, environment, clear=True):
                        result = SBP_CLIENT.run_remote_skill_pull(
                            TAILNET_REMOTE,
                            ["pull", "sample"],
                            opener=mock.Mock(side_effect=AssertionError("request attempted")),
                            stdout=output,
                        )
                    self.assertEqual(result, 1)
                    self.assertEqual(
                        json.loads(output.getvalue())["error_code"],
                        "SKILL_CACHE_IDENTITY_INVALID",
                    )
                    self.assertFalse(cache_root.exists())

    def test_private_cache_rejects_broad_existing_directory_and_oversized_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            broad = root / "broad"
            broad.mkdir(mode=0o755)
            with self.assertRaisesRegex(ValueError, "mode 0700"):
                SBP_CLIENT._private_directory(broad, create=True)
            self.assertEqual(broad.stat().st_mode & 0o777, 0o755)

            cache = root / "cache"
            cache.mkdir(mode=0o700)
            oversized = cache / "lock.json"
            oversized.write_bytes(b"x" * (SBP_CLIENT.MAX_SKILL_LOCK_BYTES + 1))
            oversized.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "invalid"):
                SBP_CLIENT._read_private_cache_entry(
                    cache,
                    "lock.json",
                    SBP_CLIENT.MAX_SKILL_LOCK_BYTES,
                )

            target = root / "target"
            target.write_bytes(b"{}")
            target.chmod(0o600)
            oversized.unlink()
            oversized.symlink_to(target)
            with self.assertRaises(OSError):
                SBP_CLIENT._read_private_cache_entry(
                    cache,
                    "lock.json",
                    SBP_CLIENT.MAX_SKILL_LOCK_BYTES,
                )

    def test_authenticated_pull_rejects_wrong_project_alias_header(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            bundle, tree_sha = self._skill_bundle(source)
            token, _claims = self._token()
            headers = self._identity_headers(tree_sha)
            headers["X-SBP-Project-Alias"] = "other/project"
            output = io.BytesIO()
            with mock.patch.dict(
                os.environ,
                {
                    "SBP_CACHE_DIR": str(root / "cache"),
                    "SBP_PROJECT_ALIAS": "build000r/skillbox",
                    "SBP_PROJECT_ID": "project-test",
                    "SBP_RESUME_ID": "resume-test",
                    "SBP_THREAD_ID": "T-test",
                    "SBP_USER_ID": "user-test",
                },
                clear=True,
            ):
                result = SBP_CLIENT.run_remote_skill_pull(
                    TAILNET_REMOTE,
                    ["pull", "sample"],
                    opener=mock.Mock(return_value=Response(bundle, headers)),
                    token_minter=self._minter(token),
                    stdout=output,
                )
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())["error_code"], "SKILL_CACHE_IDENTITY_INVALID")


class SbpDispatchTests(unittest.TestCase):
    def test_sbp_remote_dispatches_search_over_http(self) -> None:
        body = b'{"status":"ok","result":{"hits":[{"id":"box"}]}}'
        seen_paths: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen_paths.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                del args

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        env = os.environ.copy()
        env["SBP_REMOTE"] = f"http://127.0.0.1:{server.server_port}"
        result = subprocess.run(
            [str(SBP_PATH), "cass", "search", "needle with spaces"],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, body)
        self.assertEqual(
            seen_paths,
            ["/v1/cass/search?q=needle+with+spaces"],
        )

    def test_sbp_without_remote_uses_legacy_cass_wrapper_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_root = Path(temp_dir)
            (config_root / "clients").mkdir()
            scripts_dir = config_root / "scripts"
            scripts_dir.mkdir()
            wrapper = scripts_dir / "sbp_cass.py"
            wrapper.write_text(
                "import json, sys\n"
                "sys.stdout.write(json.dumps({'argv': sys.argv[1:]}, separators=(',', ':')))\n",
                encoding="utf-8",
            )
            expected = b'{"argv":["search","local query","--json"]}'
            env = os.environ.copy()
            env.pop("SBP_REMOTE", None)
            env["SKILLBOX_CONFIG_ROOT"] = str(config_root)

            result = subprocess.run(
                [str(SBP_PATH), "cass", "search", "local query", "--json"],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, expected)

    def test_sbp_remote_dispatches_skill_pull_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            helper = SbpClientUnitTests()
            bundle, tree_sha = helper._skill_bundle(Path(temp_dir))
        seen_paths: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen_paths.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("X-Skill-Tree-Sha256", tree_sha)
                self.send_header("Content-Length", str(len(bundle)))
                self.end_headers()
                self.wfile.write(bundle)

            def log_message(self, _format: str, *args: object) -> None:
                del args

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        env = os.environ.copy()
        env["SBP_REMOTE"] = f"http://127.0.0.1:{server.server_port}"
        result = subprocess.run(
            [str(SBP_PATH), "skill", "pull", "sample", "--format", "json"],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr.decode())
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema_version"], "skill-pull-result/v1")
        self.assertEqual(payload["entry_text"], "# sample\n\nUse remote policy.\n")
        self.assertEqual(seen_paths, ["/v1/skill/pull/sample"])


if __name__ == "__main__":
    unittest.main()
