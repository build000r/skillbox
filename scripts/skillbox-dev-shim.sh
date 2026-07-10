#!/usr/bin/env bash
# skillbox-dev-shim.sh - shared PATH shim for npm/pnpm/yarn/vite/next/astro.

set -euo pipefail

SCRIPT_SOURCE="${BASH_SOURCE[0]:-$0}"
SCRIPT_PATH="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$SCRIPT_SOURCE")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd -P)"
REPO_ROOT="${SKILLBOX_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"
SHIM_NAME="$(basename "$0")"
SHIM_DIR="$(cd "$(dirname "$0")" && pwd -P)"

set +e
SKILLBOX_ROOT="$REPO_ROOT" "$REPO_ROOT/scripts/guard-dev-port.sh" --shim "$SHIM_NAME" "$@"
GUARD_STATUS=$?
set -e
if [[ "$GUARD_STATUS" -ne 0 ]]; then
  exit "$GUARD_STATUS"
fi

filter_path() {
  local part
  local remaining="${PATH}:"
  local output=""
  while [[ "$remaining" == *:* ]]; do
    part="${remaining%%:*}"
    remaining="${remaining#*:}"
    if [[ -z "$part" || "$part" == "$SHIM_DIR" ]]; then
      continue
    fi
    if [[ -z "$output" ]]; then
      output="$part"
    else
      output="${output}:$part"
    fi
  done
  printf '%s' "$output"
}

REAL_PATH="$(PATH="$(filter_path)" command -v "$SHIM_NAME" || true)"
if [[ -z "$REAL_PATH" ]]; then
  echo "skillbox dev shim: real binary not found for $SHIM_NAME" >&2
  exit 127
fi

exec "$REAL_PATH" "$@"
