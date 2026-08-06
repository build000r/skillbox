#!/usr/bin/env python3
"""Thin, stdlib-only HTTP client for remote read-only sbp verbs."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import BinaryIO

DEFAULT_TIMEOUT_SECONDS = 90.0
PULL_SCHEMA = "skill-pull-result/v1"
ERROR_SCHEMA = "skill-error/v1"
SKILL_NAME_PATTERN = "^[a-z0-9][a-z0-9-]{0,127}$"


def _bundle_api() -> tuple[type[Exception], Callable[..., object], Callable[..., None]]:
    env_manager = Path(__file__).resolve().parents[2] / ".env-manager"
    env_manager_text = str(env_manager)
    if env_manager_text not in sys.path:
        sys.path.insert(0, env_manager_text)
    from runtime_manager.distribution.bundle import (  # noqa: PLC0415
        BundleError,
        unpack_skill_bundle,
        verify_bundle_contents,
    )

    return BundleError, unpack_skill_bundle, verify_bundle_contents


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


def _request_headers(accept: str) -> dict[str, str]:
    """Base headers plus optional bearer auth from SBP_TOKEN (never logged)."""
    headers = {"Accept": accept}
    token = os.environ.get("SBP_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def run_remote_cass(
    remote: str,
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., object] = urllib.request.urlopen,
    stdout: BinaryIO | None = None,
    stderr: object | None = None,
) -> int:
    """Run one remote cass read and copy the server envelope byte-for-byte."""
    output = stdout if stdout is not None else sys.stdout.buffer
    errors = stderr if stderr is not None else sys.stderr
    try:
        url = _cass_url(remote, args)
        response = opener(
            urllib.request.Request(url, headers=_request_headers("application/json")),
            timeout=timeout,
        )
        output.write(response.read())
        return 0
    except ValueError as exc:
        print(f"sbp remote: {exc}", file=errors)
        return 2
    except urllib.error.HTTPError as exc:
        body = exc.read()
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


def _skill_error(message: str) -> bytes:
    payload = {
        "ok": False,
        "schema_version": ERROR_SCHEMA,
        "error_code": "SKILL_TREE_DRIFT",
        "message": message,
        "retryable": True,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def run_remote_skill_pull(
    remote: str,
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    opener: Callable[..., object] = urllib.request.urlopen,
    stdout: BinaryIO | None = None,
    stderr: object | None = None,
) -> int:
    """Fetch, verify, and print one remote current-session skill packet."""
    output = stdout if stdout is not None else sys.stdout.buffer
    errors = stderr if stderr is not None else sys.stderr
    try:
        name = _skill_pull_name(args)
        quoted_name = urllib.parse.quote(name, safe="")
        url = f"{remote.rstrip('/')}/v1/skill/pull/{quoted_name}"
        response = opener(
            urllib.request.Request(url, headers=_request_headers("application/gzip")),
            timeout=timeout,
        )
        bundle_bytes = response.read()
        expected_tree = response.headers.get("X-Skill-Tree-Sha256", "")
    except ValueError as exc:
        print(f"sbp remote: {exc}", file=errors)
        return 2
    except urllib.error.HTTPError as exc:
        body = exc.read()
        if body:
            output.write(body)
        else:
            print(f"sbp remote: HTTP {exc.code} from {exc.url}", file=errors)
        return 1
    except (OSError, urllib.error.URLError) as exc:
        print(f"sbp remote: request failed: {exc}", file=errors)
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
    output.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    return 0


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
