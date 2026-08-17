#!/usr/bin/env bash
# Rollback assertion fixture (skillbox-dcg-upgrade-rollback-n8lu).
#
# Given a DCG home and the digests captured BEFORE an upgrade attempt, prove the
# failed upgrade left the guard exactly as it found it. Prints DCG_ROLLBACK_OK
# only when BOTH hold:
#
#   * managed DCG state is byte-identical (binary, policy, user config, hooks)
#   * an unrelated file under the same home is untouched, so a rollback that
#     "worked" by flattening the whole home is not mistaken for a clean one
#
# Usage: assert-rollback.sh <home> <expected_managed_digest> <expected_unrelated_digest>
set -euo pipefail

HOME_DIR="${1:?home required}"
EXPECT_MANAGED="${2:?expected managed digest required}"
EXPECT_UNRELATED="${3:?expected unrelated digest required}"

MANAGED_RELPATHS=(
  ".claude/settings.json"
  ".codex/hooks.json"
  ".codex/config.toml"
  ".config/dcg/config.toml"
  ".config/dcg/skillbox-reconcile.json"
)
UNRELATED_RELPATH="${DCG_UNRELATED_RELPATH:-.config/unrelated/keep.txt}"
BINARY_PATH="${SKILLBOX_DCG_BIN:-${HOME_DIR}/.local/bin/dcg}"

sha_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

sha_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
  else
    shasum -a 256 | awk '{print $1}'
  fi
}

digest_managed() {
  local relpath="" target="" digest="" material=""
  for relpath in "${MANAGED_RELPATHS[@]}"; do
    target="${HOME_DIR}/${relpath}"
    if [[ -f "${target}" ]]; then digest="$(sha_file "${target}")"; else digest="absent"; fi
    material="${material}${relpath}=${digest}"$'\n'
  done
  if [[ -f "${BINARY_PATH}" ]]; then digest="$(sha_file "${BINARY_PATH}")"; else digest="absent"; fi
  material="${material}binary=${digest}"$'\n'
  printf '%s' "${material}" | sha_stdin
}

digest_unrelated() {
  local target="${HOME_DIR}/${UNRELATED_RELPATH}"
  if [[ -f "${target}" ]]; then sha_file "${target}"; else printf '%s\n' "absent"; fi
}

actual_managed="$(digest_managed)"
actual_unrelated="$(digest_unrelated)"

rc=0
if [[ "${actual_managed}" != "${EXPECT_MANAGED}" ]]; then
  printf 'DCG_ROLLBACK_MANAGED_DRIFT expected=%s actual=%s\n' "${EXPECT_MANAGED}" "${actual_managed}" >&2
  rc=1
fi
if [[ "${actual_unrelated}" != "${EXPECT_UNRELATED}" ]]; then
  printf 'DCG_ROLLBACK_UNRELATED_DRIFT expected=%s actual=%s\n' "${EXPECT_UNRELATED}" "${actual_unrelated}" >&2
  rc=1
fi

if [[ "${rc}" -eq 0 ]]; then
  printf 'DCG_ROLLBACK_OK managed=%s unrelated=%s\n' "${actual_managed}" "${actual_unrelated}"
fi
exit "${rc}"
