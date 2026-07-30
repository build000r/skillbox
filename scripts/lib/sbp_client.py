#!/usr/bin/env python3
"""Thin, stdlib-only HTTP client for remote read-only sbp verbs."""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import BinaryIO

DEFAULT_TIMEOUT_SECONDS = 90.0


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
            urllib.request.Request(url, headers={"Accept": "application/json"}),
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


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote")
    parser.add_argument("command", choices=("cass",))
    parser.add_argument("args", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    remote = parsed.remote or env.get("SBP_REMOTE", "")
    if not remote:
        parser.error("--remote or SBP_REMOTE is required")
    return run_remote_cass(remote, parsed.args)


if __name__ == "__main__":
    raise SystemExit(main())
