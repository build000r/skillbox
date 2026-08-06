"""Thin, stdlib-only HTTP client for remote read-only sbp verbs."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import BinaryIO

DEFAULT_TIMEOUT_SECONDS = 90.0
TOKEN_TIMEOUT_SECONDS = 10.0
MAX_CASS_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SKILL_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_SKILL_LOCK_BYTES = 128 * 1024
AMP_ISSUER = "https://ampcode.com/api/workload-identity"
PULL_SCHEMA = "skill-pull-result/v1"
ERROR_SCHEMA = "skill-error/v1"
SKILL_NAME_PATTERN = "^[a-z0-9][a-z0-9-]{0,127}$"
PROJECT_ALIAS_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPEN = urllib.request.build_opener(_RejectRedirects()).open


def _bundle_api() -> tuple[type[Exception], Callable[..., object], Callable[..., None]]:
    env_manager = Path(__file__).resolve().parents[2] / ".env-manager"
    env_manager_text = str(env_manager)
    if env_manager_text not in sys.path:
        sys.path.insert(0, env_manager_text)
    from runtime_manager.distribution.bundle import (
        BundleError,
        unpack_skill_bundle,
        verify_bundle_contents,
    )

    return BundleError, unpack_skill_bundle, verify_bundle_contents


def _url_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("SBP remote origin is invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("SBP remote origin has an invalid port") from exc
    hostname = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(hostname)
        hostname = address.compressed
        host = f"[{hostname}]" if address.version == 6 else hostname
    except ValueError:
        host = hostname
    default_port = 80 if parsed.scheme == "http" else 443
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme}://{host}{suffix}"


def _canonical_remote(remote: str) -> str:
    parsed = urllib.parse.urlsplit(remote)
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("SBP remote must be an exact origin without path, query, or fragment")
    origin = _url_origin(remote)
    canonical = urllib.parse.urlsplit(origin)
    hostname = canonical.hostname or ""
    if canonical.scheme == "http":
        if hostname == "localhost":
            return origin
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError as exc:
            raise ValueError("plain HTTP SBP remote must be loopback or a Tailnet literal") from exc
        if not (address.is_loopback or address in TAILSCALE_V4 or address in TAILSCALE_V6):
            raise ValueError("plain HTTP SBP remote must be loopback or a Tailnet literal")
        return origin
    allowed = os.environ.get("SBP_ALLOWED_HTTPS_ORIGIN", "").strip()
    if not allowed:
        raise ValueError("HTTPS SBP remote requires SBP_ALLOWED_HTTPS_ORIGIN")
    allowed_parts = urllib.parse.urlsplit(allowed)
    if allowed_parts.path not in {"", "/"} or allowed_parts.query or allowed_parts.fragment:
        raise ValueError("SBP_ALLOWED_HTTPS_ORIGIN must be an exact origin")
    if _url_origin(allowed) != origin:
        raise ValueError("HTTPS SBP remote does not match SBP_ALLOWED_HTTPS_ORIGIN")
    return origin


def _response_origin_matches(response: object, remote: str) -> None:
    geturl = getattr(response, "geturl", None)
    if callable(geturl):
        final_url = geturl()
        if not isinstance(final_url, str) or _url_origin(final_url) != remote:
            raise ValueError("SBP response changed transport origin")


def _read_bounded(response: object, maximum: int, label: str) -> bytes:
    chunks: list[bytes] = []
    observed = 0
    while True:
        chunk = response.read(min(64 * 1024, maximum + 1 - observed))
        if not chunk:
            return b"".join(chunks)
        if not isinstance(chunk, bytes):
            raise TypeError(f"{label} returned a non-bytes response body")
        chunks.append(chunk)
        observed += len(chunk)
        if observed > maximum:
            raise ValueError(f"{label} exceeds the maximum response size")


def _cass_url(remote: str, args: Sequence[str]) -> str:
    normalized_args = [arg for arg in args if arg != "--json"]
    if not normalized_args:
        raise ValueError("cass requires status or search")

    verb, *verb_args = normalized_args
    base = remote.rstrip("/")

    if verb == "status":
        if verb_args:
            raise ValueError("remote cass status only supports --json")
        return f"{base}/v1/cass/status"

    if verb == "search":
        if not verb_args:
            raise ValueError("remote cass search requires a query")
        if any(arg.startswith("-") for arg in verb_args):
            raise ValueError("remote cass search v1 does not support search options")
        query = " ".join(verb_args)
        return f"{base}/v1/cass/search?{urllib.parse.urlencode({'q': query})}"

    raise ValueError(f"remote cass v1 does not support {verb!r}")


def _jwt_claims(token: str, audience: str) -> dict[str, object]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError
        header_payload = parts[0] + "=" * (-len(parts[0]) % 4)
        header = json.loads(base64.urlsafe_b64decode(header_payload))
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception as exc:
        raise ValueError("SBP_TOKEN is not a parseable JWT") from exc
    now = time.time()
    required = ("email", "project_id", "thread_id", "user_id", "jti", "sub")
    if (
        not isinstance(header, dict)
        or header.get("alg") != "RS256"
        or not isinstance(claims, dict)
        or claims.get("iss") != AMP_ISSUER
        or claims.get("aud") != audience
        or claims.get("token_use") != "exchanged"
        or claims.get("email_verified") is not True
        or any(not isinstance(claims.get(k), str) or not claims[k] for k in required)
        or isinstance(claims.get("iat"), bool)
        or isinstance(claims.get("exp"), bool)
        or not isinstance(claims.get("iat"), (int, float))
        or not isinstance(claims.get("exp"), (int, float))
        or claims["iat"] > now + 30
        or claims["exp"] <= now
        or claims["exp"] <= claims["iat"]
        or claims["exp"] - claims["iat"] > 3600
    ):
        raise ValueError("SBP_TOKEN does not satisfy the Amp identity contract")
    workspace = claims.get("workspace_id")
    if workspace is not None and (not isinstance(workspace, str) or not workspace.strip()):
        raise ValueError("SBP_TOKEN does not satisfy the Amp identity contract")
    prefix = f"workspace:{workspace}:" if workspace is not None else ""
    expected_sub = (
        f"{prefix}project:{claims['project_id']}:user:{claims['user_id']}:"
        f"thread:{claims['thread_id']}"
    )
    if claims["sub"] != expected_sub:
        raise ValueError("SBP_TOKEN subject does not match identity claims")
    expected_claims = {
        "project_id": os.environ.get("SBP_PROJECT_ID"),
        "thread_id": os.environ.get("SBP_THREAD_ID"),
        "workspace_id": os.environ.get("SBP_WORKSPACE_ID"),
    }
    if any(expected and claims.get(key) != expected for key, expected in expected_claims.items()):
        raise ValueError("SBP_TOKEN does not match expected identity")
    return claims


def _is_loopback(remote: str) -> bool:
    hostname = (urllib.parse.urlsplit(remote).hostname or "").lower()
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _auth(remote: str, *, force_mint: bool = False) -> tuple[str | None, dict[str, object] | None]:
    audience = os.environ.get("SBP_AUDIENCE", "sbpd")
    explicit = os.environ.get("SBP_TOKEN", "").strip()
    if explicit and not force_mint:
        return explicit, _jwt_claims(explicit, audience)
    if not force_mint and _is_loopback(remote) and os.environ.get("SBP_REQUIRE_AUTH") != "1":
        return None, None
    ttl = os.environ.get("SBP_TOKEN_TTL_SECONDS", "600")
    try:
        if not 60 <= int(ttl) <= 3600:
            raise ValueError
    except ValueError as exc:
        raise ValueError("SBP token TTL must be between 60 and 3600 seconds") from exc
    try:
        completed = subprocess.run(
            ["amp", "orb", "id-token", "--audience", audience, "--ttl-seconds", ttl],
            check=True, capture_output=True, text=True, timeout=TOKEN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("unable to mint Amp workload identity") from exc
    token = completed.stdout.strip()
    return token, _jwt_claims(token, audience)


def _open_authenticated(
    remote: str,
    url: str,
    accept: str,
    *,
    timeout: float,
    opener: Callable[..., object],
) -> tuple[object, dict[str, object] | None]:
    token, claims = _auth(remote)
    try:
        response = opener(
            urllib.request.Request(url, headers=_request_headers(accept, token)),
            timeout=timeout,
        )
        _response_origin_matches(response, remote)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        token, claims = _auth(remote, force_mint=True)
        response = opener(
            urllib.request.Request(url, headers=_request_headers(accept, token)),
            timeout=timeout,
        )
        _response_origin_matches(response, remote)
    return response, claims


def _request_headers(accept: str, token: str | None = None) -> dict[str, str]:
    headers = {"Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def run_remote_cass(
    remote: str,
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., object] = _NO_REDIRECT_OPEN,
    stdout: BinaryIO | None = None,
    stderr: object | None = None,
) -> int:
    """Run one remote cass read and copy the server envelope byte-for-byte."""
    output = stdout if stdout is not None else sys.stdout.buffer
    errors = stderr if stderr is not None else sys.stderr
    try:
        remote = _canonical_remote(remote)
        url = _cass_url(remote, args)
    except ValueError as exc:
        print(f"sbp remote: {exc}", file=errors)
        return 2
    try:
        response, _claims = _open_authenticated(
            remote,
            url,
            "application/json",
            timeout=timeout,
            opener=opener,
        )
        output.write(_read_bounded(response, MAX_CASS_RESPONSE_BYTES, "Cass response"))
        return 0
    except (TypeError, ValueError) as exc:
        print(f"sbp remote: {exc}", file=errors)
        return 1
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_CASS_RESPONSE_BYTES + 1)
        if body:
            output.write(body)
        else:
            print(f"sbp remote: HTTP {exc.code} from {exc.url}", file=errors)
        return 1
    except (OSError, urllib.error.URLError) as exc:
        print(f"sbp remote: request failed: {exc}", file=errors)
        return 1


def _skill_pull_name(args: Sequence[str]) -> str:
    remaining = list(args)
    if not remaining or remaining.pop(0) != "pull":
        raise ValueError("remote skill only supports pull")
    if not remaining:
        raise ValueError("remote skill pull requires a name")
    name = remaining.pop(0)

    index = 0
    while index < len(remaining):
        token = remaining[index]
        if token in {"--format", "--cwd"}:
            index += 1
            if index >= len(remaining):
                raise ValueError(f"remote skill pull {token} requires a value")
            value = remaining[index]
            if token == "--format" and value != "json":
                raise ValueError("remote skill pull only supports --format json")
        elif token.startswith("--format="):
            if token.removeprefix("--format=") != "json":
                raise ValueError("remote skill pull only supports --format json")
        elif token.startswith("--cwd="):
            if not token.removeprefix("--cwd="):
                raise ValueError("remote skill pull --cwd requires a value")
        else:
            raise ValueError(f"remote skill pull does not support {token!r}")
        index += 1

    if re.fullmatch(SKILL_NAME_PATTERN, name) is None:
        raise ValueError("skill name must match the host skill naming contract")
    return name


def _skill_error(message: str, code: str = "SKILL_TREE_DRIFT", *, retryable: bool = True) -> bytes:
    payload = {
        "ok": False,
        "schema_version": ERROR_SCHEMA,
        "error_code": code,
        "message": message,
        "retryable": retryable,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def run_remote_skill_pull(
    remote: str,
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., object] = _NO_REDIRECT_OPEN,
    stdout: BinaryIO | None = None,
    stderr: object | None = None,
) -> int:
    """Fetch, verify, and print one remote current-session skill packet."""
    output = stdout if stdout is not None else sys.stdout.buffer
    errors = stderr if stderr is not None else sys.stderr
    claims = None
    try:
        name = _skill_pull_name(args)
    except ValueError as exc:
        print(f"sbp remote: {exc}", file=errors)
        return 2
    try:
        remote = _canonical_remote(remote)
        cache_root = _cache_root()
        cache_key = hashlib.sha256(f"{remote.rstrip('/')}\0{name}".encode()).hexdigest()
        cache_dir = cache_root / cache_key
    except ValueError as exc:
        output.write(_skill_error(str(exc), "SKILL_CACHE_IDENTITY_INVALID", retryable=False))
        return 1
    try:
        if os.environ.get("SBP_OFFLINE") == "1":
            return _emit_cached(cache_dir, remote, name, output)
        quoted_name = urllib.parse.quote(name, safe="")
        url = f"{remote.rstrip('/')}/v1/skill/pull/{quoted_name}"
        response, claims = _open_authenticated(
            remote,
            url,
            "application/gzip",
            timeout=timeout,
            opener=opener,
        )
        bundle_bytes = _read_bounded(response, MAX_SKILL_BUNDLE_BYTES, "skill bundle")
        expected_tree = response.headers.get("X-Skill-Tree-Sha256", "")
    except (TypeError, ValueError) as exc:
        output.write(_skill_error(str(exc), "SKILL_TRANSPORT_AUTH_UNAVAILABLE"))
        return 1
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_CASS_RESPONSE_BYTES + 1)
        if body:
            output.write(body)
        else:
            print(f"sbp remote: HTTP {exc.code} from {exc.url}", file=errors)
        return 1
    except (OSError, urllib.error.URLError):
        output.write(_skill_error("Authenticated skill transport is unavailable.", "SKILL_TRANSPORT_UNAVAILABLE"))
        return 1

    BundleError, unpack_skill_bundle, verify_bundle_contents = _bundle_api()
    try:
        with tempfile.TemporaryDirectory(prefix="sbp-remote-skill-") as temporary:
            destination = Path(temporary) / name
            destination.mkdir()
            manifest = unpack_skill_bundle(io.BytesIO(bundle_bytes), destination)
            verify_bundle_contents(manifest, destination)
            if manifest.name != name or expected_tree != manifest.tree_sha256:
                raise BundleError("server and bundle skill identities do not match")
            entry_payload = (destination / "SKILL.md").read_bytes()
            entry_text = entry_payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        output.write(_skill_error("Remote skill entry is not valid UTF-8."))
        return 1
    except (BundleError, OSError, EOFError) as exc:
        output.write(_skill_error(f"Remote skill bundle verification failed: {exc}"))
        return 1

    transport = None
    if claims is not None:
        try:
            transport = _cache_verified(cache_dir, remote, name, bundle_bytes, manifest, response.headers, claims)
        except ValueError as exc:
            output.write(_skill_error(str(exc), "SKILL_CACHE_IDENTITY_INVALID", retryable=False))
            return 1

    payload = {
        "ok": True,
        "schema_version": PULL_SCHEMA,
        "name": name,
        "lifecycle": "active",
        "entry_text": entry_text,
        "tree_sha256": manifest.tree_sha256,
        "entry_sha256": hashlib.sha256(entry_payload).hexdigest(),
        "receipt_sha256": hashlib.sha256(bundle_bytes).hexdigest(),
        "source_classification": "host-canonical",
        "instructions": "use this content immediately in the current session",
    }
    if transport is not None:
        payload["transport_receipt"] = transport
    output.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    return 0


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _resume_id() -> str:
    value = os.environ.get("SBP_RESUME_ID", "").strip()
    if not value:
        try:
            value = (
                _repo_root() / ".skillbox-state/project-orb/hook-state/orb-resume-id"
            ).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    if not value:
        raise ValueError("authenticated cache requires a stable resume identity")
    return value


def _repo_root() -> Path:
    cwd = Path.cwd().resolve()
    return next(
        (candidate for candidate in (cwd, *cwd.parents) if (candidate / ".git").exists()),
        cwd,
    )


def _cache_root() -> Path:
    repo_root = _repo_root()
    path = Path(
        os.environ.get("SBP_CACHE_DIR", repo_root / ".skillbox-state/orb-skill-cache")
    ).absolute()
    if path.is_symlink():
        raise ValueError("skill cache root cannot be a symlink")
    resolved = path.resolve(strict=False)
    paths = (path.absolute(), resolved)
    if any(
        any(
            left in {".agents", ".claude", ".codex"} and right == "skills"
            for left, right in zip(candidate.parts, candidate.parts[1:], strict=False)
        )
        for candidate in paths
    ):
        raise ValueError("skill cache must remain outside agent discovery roots")
    return path


def _private_directory(path: Path, *, create: bool) -> os.stat_result:
    created = False
    if not os.path.lexists(path):
        if not create:
            raise ValueError("cache path is missing")
        path.mkdir(parents=True, mode=0o700)
        created = True
    if path.is_symlink() or not path.is_dir():
        raise ValueError("cache path is not a private directory")
    details = path.stat()
    if created:
        path.chmod(0o700)
        details = path.stat()
    if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o700:
        raise ValueError("existing cache directories must be owned by this user and mode 0700")
    return details


def _read_private_cache_entry(cache_dir: Path, filename: str, maximum: int) -> bytes:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(cache_dir, directory_flags)
    try:
        directory_details = os.fstat(directory_fd)
        if (
            directory_details.st_uid != os.geteuid()
            or stat.S_IMODE(directory_details.st_mode) != 0o700
        ):
            raise ValueError("cache directory is not private")
        file_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            details = os.fstat(file_fd)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.geteuid()
                or stat.S_IMODE(details.st_mode) != 0o600
                or details.st_size > maximum
            ):
                raise ValueError("cache entry is invalid")
            chunks: list[bytes] = []
            observed = 0
            while True:
                chunk = os.read(file_fd, min(64 * 1024, maximum + 1 - observed))
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
                observed += len(chunk)
                if observed > maximum:
                    raise ValueError("cache entry exceeds its size limit")
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def _cache_verified(
    cache_dir: Path,
    remote: str,
    name: str,
    bundle: bytes,
    manifest: object,
    headers: Mapping[str, str],
    claims: Mapping[str, object],
) -> dict[str, object]:
    header_names = (
        "X-SBP-Project-Id",
        "X-SBP-Project-Alias",
        "X-SBP-Remote-Sha",
        "X-SBP-Resolution-Receipt",
        "X-SBP-Lease-Id",
        "X-SBP-Thread-Id",
        "X-SBP-User-Id",
        "X-SBP-Visibility",
    )
    fields = {key: headers.get(key, "") for key in header_names}
    expected_alias = os.environ.get("SBP_PROJECT_ALIAS", "").strip()
    if not expected_alias:
        raise ValueError("authenticated cache requires SBP_PROJECT_ALIAS")
    if re.fullmatch(PROJECT_ALIAS_PATTERN, expected_alias) is None:
        raise ValueError("SBP_PROJECT_ALIAS has invalid characters")
    if (
        fields["X-SBP-Project-Id"] != claims["project_id"]
        or fields["X-SBP-Thread-Id"] != claims["thread_id"]
        or fields["X-SBP-User-Id"] != claims["user_id"]
        or fields["X-SBP-Lease-Id"] != claims["jti"]
        or fields["X-SBP-Project-Alias"] != expected_alias
    ):
        raise ValueError("server identity headers do not match minted token")
    remote_sha = fields["X-SBP-Remote-Sha"]
    receipt = fields["X-SBP-Resolution-Receipt"]
    if (
        fields["X-SBP-Visibility"] != "private"
        or re.fullmatch(r"[0-9a-f]{40}", remote_sha) is None
        or re.fullmatch(r"[0-9a-f]{64}", receipt) is None
    ):
        raise ValueError("server cache identity is incomplete")
    lock = {
        "schema_version": "sbp-orb-skill-lock/v1",
        "name": name,
        "project_alias": expected_alias,
        "project_id": claims["project_id"],
        "remote_sha256": hashlib.sha256(remote.rstrip("/").encode()).hexdigest(),
        "remote_pinned_sha": remote_sha,
        "capsule_sha256": hashlib.sha256(bundle).hexdigest(),
        "tree_sha256": manifest.tree_sha256,
        "thread_id": claims["thread_id"],
        "user_id": claims["user_id"],
        "workspace_id": claims.get("workspace_id"),
        "subject": claims["sub"],
        "resume_id": _resume_id(),
        "lease_id": claims["jti"],
        "resolution_receipt": receipt,
        "visibility": "private",
    }
    lock["lock_sha256"] = hashlib.sha256(_canonical(lock)).hexdigest()
    _private_directory(cache_dir.parent, create=True)
    _private_directory(cache_dir, create=True)
    for filename, body in (("capsule.tar.gz", bundle), ("lock.json", _canonical(lock))):
        fd, temporary = tempfile.mkstemp(dir=cache_dir, prefix=".tmp-")
        try:
            destination = cache_dir / filename
            if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                raise ValueError("cache entry is not a regular file")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(body)
                stream.flush()
            os.fsync(fd)
            os.close(fd)
            os.replace(temporary, destination)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return {"cache": "private", "lock_sha256": lock["lock_sha256"]}


def _emit_cached(cache_dir: Path, remote: str, name: str, output: BinaryIO) -> int:
    try:
        expected = {
            "project_id": os.environ["SBP_PROJECT_ID"].strip(),
            "project_alias": os.environ["SBP_PROJECT_ALIAS"].strip(),
            "thread_id": os.environ["SBP_THREAD_ID"].strip(),
            "user_id": os.environ["SBP_USER_ID"].strip(),
        }
        if not all(expected.values()):
            raise ValueError("offline cache identity is incomplete")
        if re.fullmatch(PROJECT_ALIAS_PATTERN, expected["project_alias"]) is None:
            raise ValueError("offline project alias is invalid")
        workspace = os.environ.get("SBP_WORKSPACE_ID", "").strip() or None
        subject_prefix = f"workspace:{workspace}:" if workspace is not None else ""
        subject = (
            f"{subject_prefix}project:{expected['project_id']}:user:{expected['user_id']}:"
            f"thread:{expected['thread_id']}"
        )
        _private_directory(cache_dir, create=False)
        lock = json.loads(
            _read_private_cache_entry(cache_dir, "lock.json", MAX_SKILL_LOCK_BYTES)
        )
        bundle = _read_private_cache_entry(
            cache_dir,
            "capsule.tar.gz",
            MAX_SKILL_BUNDLE_BYTES,
        )
        if not isinstance(lock, dict):
            raise TypeError("cache lock is invalid")
        claimed = lock.pop("lock_sha256")
        checks = (
            lock.get("schema_version") == "sbp-orb-skill-lock/v1",
            claimed == hashlib.sha256(_canonical(lock)).hexdigest(),
            lock.get("capsule_sha256") == hashlib.sha256(bundle).hexdigest(),
            lock.get("remote_sha256") == hashlib.sha256(remote.rstrip("/").encode()).hexdigest(),
            lock.get("name") == name,
            lock.get("visibility") == "private",
            lock.get("resume_id") == _resume_id(),
            all(lock.get(key) == value for key, value in expected.items()),
            lock.get("workspace_id") == workspace,
            lock.get("subject") == subject,
            bool(lock.get("lease_id")),
            re.fullmatch(r"[0-9a-f]{40}", lock.get("remote_pinned_sha", "")) is not None,
            re.fullmatch(r"[0-9a-f]{64}", lock.get("resolution_receipt", "")) is not None,
        )
        if not all(checks):
            raise ValueError("cache lock verification failed")
        _BundleError, unpack, verify = _bundle_api()
        with tempfile.TemporaryDirectory(prefix="sbp-cache-verify-") as temporary:
            destination = Path(temporary) / name
            destination.mkdir()
            manifest = unpack(io.BytesIO(bundle), destination)
            verify(manifest, destination)
            if manifest.tree_sha256 != lock.get("tree_sha256"):
                raise ValueError("cache tree verification failed")
            entry = (destination / "SKILL.md").read_bytes()
        payload = {
            "ok": True,
            "schema_version": PULL_SCHEMA,
            "name": name,
            "lifecycle": "active",
            "entry_text": entry.decode(),
            "tree_sha256": manifest.tree_sha256,
            "entry_sha256": hashlib.sha256(entry).hexdigest(),
            "receipt_sha256": hashlib.sha256(bundle).hexdigest(),
            "source_classification": "host-canonical",
            "instructions": "use this content immediately in the current session",
            "transport_receipt": {"cache": "private-offline", "lock_sha256": claimed},
        }
        output.write(_canonical(payload) + b"\n")
        return 0
    except Exception:  # noqa: BLE001 -- every cache parse/verification failure is fail-closed
        output.write(
            _skill_error(
                "No fully verified private skill cache is available.",
                "SKILL_CACHE_UNAVAILABLE",
                retryable=False,
            )
        )
        return 1


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote")
    parser.add_argument("command", choices=("cass", "skill"))
    parser.add_argument("args", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    remote = parsed.remote or env.get("SBP_REMOTE", "")
    if not remote:
        parser.error("--remote or SBP_REMOTE is required")
    if parsed.command == "cass":
        return run_remote_cass(remote, parsed.args)
    return run_remote_skill_pull(remote, parsed.args)


if __name__ == "__main__":
    raise SystemExit(main())
