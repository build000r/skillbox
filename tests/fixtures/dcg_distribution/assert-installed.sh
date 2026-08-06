#!/usr/bin/env bash
# Assert that the DCG binary at $DCG_BIN is the pinned, verified build.
#
# Fails closed and non-zero on: a missing/non-executable binary, an unsupported
# platform, a version that is not the repo pin, or a stale `dcg mcp` MCP bridge.
# On success prints exactly one machine-greppable line:
#
#   DCG_DISTRIBUTION_OK version=v0.6.7 asset=<asset> sha256=<sha256> minisign_key_id=<id> mcp_command=mcp-server
#
# Usage:
#   DCG_BIN=/path/to/dcg tests/fixtures/dcg_distribution/assert-installed.sh
#   DCG_SKIP_MCP=1 ...   # skip the bounded stdio handshake (digest checks only)
set -euo pipefail

FIXTURE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${FIXTURE_DIR}/../../.." && pwd)"
DCG_BIN="${DCG_BIN:-}"

if [[ -z "${DCG_BIN}" ]]; then
  echo "DCG_DISTRIBUTION_FAIL reason=DCG_BIN_UNSET" >&2
  echo "remediation: DCG_BIN=<path> $0" >&2
  exit 2
fi

if [[ ! -x "${DCG_BIN}" ]]; then
  echo "DCG_DISTRIBUTION_FAIL reason=DCG_BINARY_MISSING path=${DCG_BIN}" >&2
  echo "remediation: python3 .env-manager/manage.py sync --profile core" >&2
  exit 3
fi

PYTHONPATH="${REPO_ROOT}/.env-manager${PYTHONPATH:+:${PYTHONPATH}}" \
DCG_BIN="${DCG_BIN}" \
DCG_SKIP_MCP="${DCG_SKIP_MCP:-}" \
python3 - <<'PY'
import os
import sys

from runtime_manager import dcg_distribution as dist

binary = os.environ["DCG_BIN"]

try:
    record = dist.provenance_record(binary)
except dist.DcgDistributionError as exc:
    print(f"DCG_DISTRIBUTION_FAIL reason={exc.code} detail={exc.message}", file=sys.stderr)
    for action in exc.next_actions or ["python3 .env-manager/manage.py sync --profile core"]:
        print(f"remediation: {action}", file=sys.stderr)
    raise SystemExit(4)

if record["version"] != dist.DCG_VERSION or not record["verified"]:
    print(
        "DCG_DISTRIBUTION_FAIL reason=DCG_VERSION_MISMATCH "
        f"installed={record['installed_version']} expected={dist.DCG_VERSION}",
        file=sys.stderr,
    )
    print("remediation: python3 .env-manager/manage.py sync --profile core", file=sys.stderr)
    raise SystemExit(5)

if not os.environ.get("DCG_SKIP_MCP"):
    report = dist.mcp_readiness_report(binary)
    if not report["ready"]:
        print(
            "DCG_DISTRIBUTION_FAIL reason=DCG_MCP_NOT_READY "
            f"command={report['command']} detail={report['current'].get('reason', '')}",
            file=sys.stderr,
        )
        print(
            f"remediation: run `{binary} {dist.DCG_MCP_COMMAND}` and confirm the "
            "stdio initialize handshake responds",
            file=sys.stderr,
        )
        raise SystemExit(6)

print(
    "DCG_DISTRIBUTION_OK"
    f" version={record['version']}"
    f" asset={record['asset']}"
    f" sha256={record['sha256']}"
    f" minisign_key_id={record['minisign_key_id']}"
    f" cache_key={record['cache_key']}"
    f" mcp_command={record['mcp_command']}"
)
PY
