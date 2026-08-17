#!/usr/bin/env bash
set -euo pipefail

ARCHIVE=""
ARCHIVE_SHA256=""
REPO_DIR=""
CLIENT_ID=""
ROLLBACK_DIR=""
TEMP_DIR=""
LOCK_DIR=""
PRESERVE_ROOT=""
SWAPPED=0
SUCCESS=0
STOPPED_OLD=0
PROFILE_ARGS=()

# NOTE: .skillbox-state/operator/ is the canonical home for operator secrets
# (.env, .env.box) — it lives out of the workspace bind mount and is preserved
# transitively via the .skillbox-state entry below. The legacy repo-root .env /
# .env.box entries remain so upgrades don't clobber not-yet-migrated operators.
PRESERVE_PATHS=(
  ".env"
  ".env.box"
  ".mcp.json"
  ".skillbox-state"
  "workspace/.compose-overrides"
  "workspace/.focus.json"
  "workspace/boxes.json"
  "workspace/skill-repos"
  "workspace/skill-repos.lock.json"
)

# --- DCG transaction (skillbox-dcg-upgrade-rollback-n8lu) ---------------------
# DCG guards the agent's shell. An upgrade that leaves it degraded is worse than
# an upgrade that fails, because the host keeps working while nothing is
# guarding it. So DCG joins the same transaction as the checkout:
#
#   1. DCG state is captured only AFTER the release archive verifies -- an
#      unverified artifact never gets to touch the guard.
#   2. DCG is re-validated BEFORE the upgrade is declared successful.
#   3. Any failure restores the prior binary, policy, user config and hooks.
#
# The bundle is a plain tar plus a sha256, deliberately NOT the new release's
# own `dcg_reconcile.rollback`. If the upgrade is what broke DCG, the new
# release's rollback path is exactly the code you cannot trust to undo it; this
# bundle is restorable with nothing but shell and tar.
DCG_HOME="${SKILLBOX_DCG_HOME:-${HOME:-}}"
DCG_BIN="${SKILLBOX_DCG_BIN:-}"
DCG_BUNDLE=""
DCG_BUNDLE_SHA256=""
DCG_CAPTURED=0
DCG_BEFORE_STATE=""
RECEIPT_PATH=""
BEFORE_VERSION=""
AFTER_VERSION=""

# Every path the reconciler is allowed to write, relative to the managed home
# (runtime_manager/dcg_reconcile.py). `.codex/config.toml` is read-only to the
# reconciler but carries Codex's persisted hook TRUST, so losing it silently
# disarms the hook -- it is captured and restored with the rest.
DCG_MANAGED_RELPATHS=(
  ".claude/settings.json"
  ".codex/hooks.json"
  ".codex/config.toml"
  ".config/dcg/config.toml"
  ".config/dcg/skillbox-reconcile.json"
)

usage() {
  cat <<'EOF'
Usage: 06-upgrade-release.sh --archive <path> --sha256 <hex> --repo-dir <path> --client <id>
                            [--profile <name>] [--rollback-dir <path>] [--receipt <path>]

DCG (skillbox-dcg-upgrade-rollback-n8lu): the guard joins this transaction.
Managed DCG state is captured only after the archive digest verifies, DCG is
re-validated before success, and any failure restores the prior binary, policy,
user config and hooks from a self-contained tar bundle.
EOF
}

info() {
  printf '%s\n' "-> $*"
}

warn() {
  printf '%s\n' "WARN $*" >&2
}

err() {
  printf '%s\n' "ERR $*" >&2
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

require_cmd() {
  if ! have_cmd "$1"; then
    err "Missing required command: $1"
    exit 1
  fi
}

compose_layer_for_repo() {
  local repo_dir="$1"
  local focus_client=""
  local override=""

  repo_dir="$(cd "${repo_dir}" && pwd -P)"
  focus_client="$(cd "${repo_dir}" && python3 -c "import json; print(json.load(open('workspace/.focus.json')).get('client_id',''))" 2>/dev/null || true)"
  override="${repo_dir}/workspace/.compose-overrides/docker-compose.client-${focus_client}.yml"
  if [[ -n "${focus_client}" && -f "${override}" ]]; then
    printf '%s\n' "${override}"
    return 0
  fi
  printf '%s\n' "${repo_dir}/docker-compose.monoserver.yml"
}

repo_lifecycle_target() {
  local repo_dir="$1"
  local target="$2"
  local layer=""

  repo_dir="$(cd "${repo_dir}" && pwd -P)"
  if have_cmd make && [[ -f "${repo_dir}/Makefile" ]]; then
    (cd "${repo_dir}" && make "${target}" >/dev/null)
    return $?
  fi

  require_cmd docker
  layer="$(compose_layer_for_repo "${repo_dir}")"
  case "${target}" in
    down)
      (cd "${repo_dir}" && docker compose -f docker-compose.yml -f "${layer}" down >/dev/null)
      ;;
    build)
      (cd "${repo_dir}" && docker compose -f docker-compose.yml -f "${layer}" build >/dev/null)
      ;;
    up)
      (cd "${repo_dir}" && docker compose -f docker-compose.yml -f "${layer}" up -d workspace >/dev/null)
      ;;
    *)
      err "Unsupported lifecycle target: ${target}"
      return 1
      ;;
  esac
}

sha256_file() {
  local path="$1"
  if have_cmd sha256sum; then
    sha256sum "${path}" | awk '{print $1}'
  else
    shasum -a 256 "${path}" | awk '{print $1}'
  fi
}

sha256_stdin() {
  if have_cmd sha256sum; then
    sha256sum | awk '{print $1}'
  else
    shasum -a 256 | awk '{print $1}'
  fi
}

dcg_binary_path() {
  if [[ -n "${DCG_BIN}" ]]; then
    printf '%s\n' "${DCG_BIN}"
    return 0
  fi
  printf '%s\n' "${DCG_HOME}/.local/bin/dcg"
}

# Deterministic digest of the managed DCG state. Absent files are recorded as
# "absent" rather than skipped, so "the hook file disappeared" is a state CHANGE
# and not an invisible no-op.
dcg_state_digest() {
  local relpath="" target="" digest="" line=""
  local material=""
  for relpath in "${DCG_MANAGED_RELPATHS[@]}"; do
    target="${DCG_HOME}/${relpath}"
    if [[ -f "${target}" ]]; then
      digest="$(sha256_file "${target}")"
    else
      digest="absent"
    fi
    line="${relpath}=${digest}"
    material="${material}${line}"$'\n'
  done
  target="$(dcg_binary_path)"
  if [[ -f "${target}" ]]; then
    digest="$(sha256_file "${target}")"
  else
    digest="absent"
  fi
  material="${material}binary=${digest}"$'\n'
  printf '%s' "${material}" | sha256_stdin
}

dcg_hook_state_digest() {
  local relpath="" target="" digest="" material=""
  for relpath in ".claude/settings.json" ".codex/hooks.json" ".codex/config.toml"; do
    target="${DCG_HOME}/${relpath}"
    if [[ -f "${target}" ]]; then
      digest="$(sha256_file "${target}")"
    else
      digest="absent"
    fi
    material="${material}${relpath}=${digest}"$'\n'
  done
  printf '%s' "${material}" | sha256_stdin
}

dcg_file_digest_or_absent() {
  local target="$1"
  if [[ -f "${target}" ]]; then
    sha256_file "${target}"
    return 0
  fi
  printf '%s\n' "absent"
}

capture_dcg_bundle() {
  local relpath="" target="" staged="" binary=""
  if [[ -z "${DCG_HOME}" || ! -d "${DCG_HOME}" ]]; then
    warn "no DCG home resolved; skipping DCG transaction capture"
    return 0
  fi

  staged="${TEMP_DIR}/dcg-bundle"
  mkdir -p "${staged}"
  for relpath in "${DCG_MANAGED_RELPATHS[@]}"; do
    target="${DCG_HOME}/${relpath}"
    [[ -f "${target}" ]] || continue
    mkdir -p "${staged}/$(dirname "${relpath}")"
    cp -p "${target}" "${staged}/${relpath}"
  done
  binary="$(dcg_binary_path)"
  if [[ -f "${binary}" ]]; then
    mkdir -p "${staged}/binary"
    cp -p "${binary}" "${staged}/binary/dcg"
    printf '%s\n' "${binary}" >"${staged}/binary/path"
  fi

  DCG_BUNDLE="${TEMP_DIR}/dcg-rollback.tar"
  # Sorted + fixed metadata so the bundle digest identifies CONTENT, not the
  # moment it was taken.
  (cd "${staged}" && find . -print0 | LC_ALL=C sort -z \
    | tar --null -T - -cf "${DCG_BUNDLE}" --no-recursion 2>/dev/null) \
    || (cd "${staged}" && tar -cf "${DCG_BUNDLE}" .)
  DCG_BUNDLE_SHA256="$(sha256_file "${DCG_BUNDLE}")"
  DCG_BEFORE_STATE="$(dcg_state_digest)"
  DCG_CAPTURED=1
  info "Captured DCG rollback bundle (${DCG_BUNDLE_SHA256})"
}

restore_dcg_bundle() {
  local relpath="" target="" staged="" binary="" recorded=""
  [[ "${DCG_CAPTURED}" -eq 1 ]] || return 0
  [[ -n "${DCG_BUNDLE}" && -f "${DCG_BUNDLE}" ]] || return 0

  staged="${TEMP_DIR}/dcg-restore"
  rm -rf "${staged}"
  mkdir -p "${staged}"
  tar -xf "${DCG_BUNDLE}" -C "${staged}" || {
    err "DCG rollback bundle could not be extracted; managed state left as-is"
    return 1
  }

  # Remove first, then restore: a file that did NOT exist before must not
  # survive the rollback just because nothing overwrote it.
  # Minimal writes: a file that already matches the captured bytes is left
  # alone. Rollback should touch exactly what the upgrade disturbed, so a run
  # that changed nothing restores nothing.
  for relpath in "${DCG_MANAGED_RELPATHS[@]}"; do
    target="${DCG_HOME}/${relpath}"
    if [[ -f "${staged}/${relpath}" ]]; then
      if [[ -f "${target}" ]] \
        && [[ "$(sha256_file "${target}")" == "$(sha256_file "${staged}/${relpath}")" ]]; then
        continue
      fi
      mkdir -p "$(dirname "${target}")"
      cp -p "${staged}/${relpath}" "${target}"
    elif [[ -f "${target}" ]]; then
      rm -f "${target}"
    fi
  done

  binary="$(dcg_binary_path)"
  recorded=""
  if [[ -f "${staged}/binary/path" ]]; then
    recorded="$(cat "${staged}/binary/path")"
  fi
  if [[ -f "${staged}/binary/dcg" ]]; then
    mkdir -p "$(dirname "${binary}")"
    cp -p "${staged}/binary/dcg" "${binary}"
    if [[ -n "${recorded}" && "${recorded}" != "${binary}" && -f "${recorded}" ]]; then
      cp -p "${staged}/binary/dcg" "${recorded}"
    fi
  elif [[ -f "${binary}" ]]; then
    rm -f "${binary}"
  fi

  # Prove the restore, do not assume it. If the post-restore digest differs from
  # what was captured, the guard is NOT back in its prior state and saying
  # "rolled back" would be the most dangerous kind of false green.
  local after_state=""
  after_state="$(dcg_state_digest)"
  if [[ "${after_state}" == "${DCG_BEFORE_STATE}" ]]; then
    info "Restored DCG managed state from rollback bundle (verified ${after_state})"
    return 0
  fi
  err "DCG rollback did NOT restore the prior managed state"
  err "  expected ${DCG_BEFORE_STATE}"
  err "  actual   ${after_state}"
  return 1
}

run_dcg_action() {
  local action="$1"
  local profile=""
  local cmd=()
  cmd=(python3 ".env-manager/manage.py" "dcg-reconcile"
       "--action" "${action}" "--entrypoint" "install" "--scope" "host"
       "--client" "${CLIENT_ID}")
  for profile in "${PROFILE_ARGS[@]}"; do
    cmd+=("--profile" "${profile}")
  done
  (cd "${REPO_DIR}" && "${cmd[@]}" >/dev/null)
}

write_upgrade_receipt() {
  local receipt="${RECEIPT_PATH}"
  local policy_sha="" binary_sha="" hook_sha=""
  [[ -n "${receipt}" ]] || return 0

  policy_sha="$(dcg_file_digest_or_absent "${DCG_HOME}/.config/dcg/config.toml")"
  binary_sha="$(dcg_file_digest_or_absent "$(dcg_binary_path)")"
  hook_sha="$(dcg_hook_state_digest)"

  mkdir -p "$(dirname "${receipt}")"
  cat >"${receipt}" <<RECEIPT
{
  "schema": "skillbox.upgrade.receipt/1",
  "before_version": "${BEFORE_VERSION}",
  "after_version": "${AFTER_VERSION}",
  "binary_sha256": "${binary_sha}",
  "policy_sha256": "${policy_sha}",
  "hook_state_sha256": "${hook_sha}",
  "rollback_bundle_sha256": "${DCG_BUNDLE_SHA256}",
  "dcg_state_sha256": "$(dcg_state_digest)",
  "unchanged": $(if [[ "${BEFORE_VERSION}" == "${AFTER_VERSION}" ]]; then printf 'true'; else printf 'false'; fi)
}
RECEIPT
  info "Wrote upgrade receipt: ${receipt}"
}

verify_archive() {
  local actual=""
  if [[ ! -f "${ARCHIVE}" ]]; then
    err "Upgrade archive not found: ${ARCHIVE}"
    exit 1
  fi
  actual="$(sha256_file "${ARCHIVE}")"
  if [[ "${actual}" != "${ARCHIVE_SHA256}" ]]; then
    err "Upgrade archive SHA256 mismatch"
    err "Expected: ${ARCHIVE_SHA256}"
    err "Actual:   ${actual}"
    exit 1
  fi
}

move_preserved_paths() {
  local from_root="$1"
  local to_root="$2"
  local rel=""
  local src=""
  local dest=""

  for rel in "${PRESERVE_PATHS[@]}"; do
    src="${from_root}/${rel}"
    dest="${to_root}/${rel}"
    if [[ ! -e "${src}" ]]; then
      continue
    fi
    mkdir -p "$(dirname "${dest}")"
    rm -rf "${dest}"
    mv "${src}" "${dest}"
  done
}

restore_preserved_paths() {
  local from_root="$1"
  local to_root="$2"
  move_preserved_paths "${from_root}" "${to_root}"
}

bring_repo_up() {
  local repo_dir="$1"
  if [[ ! -d "${repo_dir}" ]]; then
    return 0
  fi
  if ! repo_lifecycle_target "${repo_dir}" up; then
    warn "Failed to restart services in ${repo_dir}"
    return 1
  fi
  return 0
}

# Shares install.sh's lock path so an upgrade cannot race a concurrent
# install (or another upgrade) mutating the same host.
acquire_lock() {
  local base="${TMPDIR:-/tmp}"
  local candidate="${base}/skillbox-install.lock"
  local holder_pid=""

  # Only assign LOCK_DIR once we own the lock; release_lock must never remove
  # a lock directory held by another running install/upgrade.
  if mkdir "${candidate}" 2>/dev/null; then
    LOCK_DIR="${candidate}"
    printf '%s\n' "$$" >"${LOCK_DIR}/pid"
    return 0
  fi

  holder_pid="$(cat "${candidate}/pid" 2>/dev/null || true)"
  if [[ -n "${holder_pid}" ]] && ! kill -0 "${holder_pid}" 2>/dev/null; then
    warn "Reclaiming stale install lock left by exited process ${holder_pid}."
    rm -rf "${candidate}"
    if mkdir "${candidate}" 2>/dev/null; then
      LOCK_DIR="${candidate}"
      printf '%s\n' "$$" >"${LOCK_DIR}/pid"
      return 0
    fi
  fi

  err "Another skillbox install/upgrade appears to be running (${candidate}${holder_pid:+, pid ${holder_pid}})."
  err "If no other install is running, remove the lock with: rm -rf ${candidate}"
  exit 1
}

release_lock() {
  if [[ -n "${LOCK_DIR}" && -d "${LOCK_DIR}" ]]; then
    rm -rf "${LOCK_DIR}" >/dev/null 2>&1 || true
  fi
}

rollback() {
  local status="$1"

  if [[ "${SUCCESS}" -eq 1 ]]; then
    if [[ -n "${TEMP_DIR}" && -d "${TEMP_DIR}" ]]; then
      rm -rf "${TEMP_DIR}"
    fi
    release_lock
    return
  fi

  # DCG first: the guard must be back before anything else is attempted, and
  # this path must not depend on the half-upgraded checkout being usable.
  restore_dcg_bundle || true

  if [[ -n "${PRESERVE_ROOT}" && -d "${PRESERVE_ROOT}" ]]; then
    if [[ "${SWAPPED}" -eq 1 && -d "${REPO_DIR}" ]]; then
      move_preserved_paths "${REPO_DIR}" "${PRESERVE_ROOT}" || true
    fi
    if [[ "${SWAPPED}" -eq 1 && -d "${REPO_DIR}" ]]; then
      repo_lifecycle_target "${REPO_DIR}" down >/dev/null 2>&1 || true
      rm -rf "${REPO_DIR}"
    fi
    if [[ "${SWAPPED}" -eq 1 && -d "${ROLLBACK_DIR}" ]]; then
      mv "${ROLLBACK_DIR}" "${REPO_DIR}"
      restore_preserved_paths "${PRESERVE_ROOT}" "${REPO_DIR}" || true
      bring_repo_up "${REPO_DIR}" || true
    elif [[ "${STOPPED_OLD}" -eq 1 && -d "${REPO_DIR}" ]]; then
      restore_preserved_paths "${PRESERVE_ROOT}" "${REPO_DIR}" || true
      bring_repo_up "${REPO_DIR}" || true
    fi
  fi

  if [[ -n "${TEMP_DIR}" && -d "${TEMP_DIR}" ]]; then
    rm -rf "${TEMP_DIR}"
  fi

  release_lock
  exit "${status}"
}

trap 'rollback $?' EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive)
      ARCHIVE="$2"
      shift 2
      ;;
    --sha256)
      ARCHIVE_SHA256="$2"
      shift 2
      ;;
    --repo-dir)
      REPO_DIR="$2"
      shift 2
      ;;
    --client)
      CLIENT_ID="$2"
      shift 2
      ;;
    --profile)
      PROFILE_ARGS+=("$2")
      shift 2
      ;;
    --rollback-dir)
      ROLLBACK_DIR="$2"
      shift 2
      ;;
    --receipt)
      RECEIPT_PATH="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      err "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

if [[ -z "${ARCHIVE}" || -z "${ARCHIVE_SHA256}" || -z "${REPO_DIR}" || -z "${CLIENT_ID}" ]]; then
  usage
  exit 1
fi

require_cmd python3
require_cmd tar
if ! have_cmd shasum && ! have_cmd sha256sum; then
  err "Need shasum or sha256sum to verify upgrade archives."
  exit 1
fi

verify_archive

if [[ ! -d "${REPO_DIR}" ]]; then
  err "Existing checkout not found: ${REPO_DIR}"
  exit 1
fi
if [[ ! -f "${REPO_DIR}/.env-manager/manage.py" ]]; then
  err "Existing checkout is missing .env-manager/manage.py: ${REPO_DIR}"
  exit 1
fi

if [[ -z "${ROLLBACK_DIR}" ]]; then
  ROLLBACK_DIR="${REPO_DIR}.rollback"
fi

acquire_lock

TEMP_DIR="$(mktemp -d)"
PRESERVE_ROOT="${TEMP_DIR}/preserve"
mkdir -p "${PRESERVE_ROOT}" "${TEMP_DIR}/extract"

# Only now, with the archive digest verified, may the transaction touch the
# guard. Capturing earlier would mean an unverified artifact had already
# influenced DCG state.
capture_dcg_bundle

if [[ -f "${REPO_DIR}/VERSION.txt" ]]; then
  BEFORE_VERSION="$(tr -d '\n' <"${REPO_DIR}/VERSION.txt")"
fi

info "Extracting release archive"
tar -xzf "${ARCHIVE}" -C "${TEMP_DIR}/extract"
STAGED_REPO="$(find "${TEMP_DIR}/extract" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
if [[ -z "${STAGED_REPO}" || ! -f "${STAGED_REPO}/.env-manager/manage.py" ]]; then
  err "Release archive does not contain a skillbox checkout"
  exit 1
fi

info "Stopping current services"
if repo_lifecycle_target "${REPO_DIR}" down >/dev/null 2>&1; then
  STOPPED_OLD=1
else
  warn "service stop failed in ${REPO_DIR}; continuing with transactional swap"
fi

info "Moving runtime-owned state out of the current checkout"
move_preserved_paths "${REPO_DIR}" "${PRESERVE_ROOT}"

rm -rf "${ROLLBACK_DIR}"
mv "${REPO_DIR}" "${ROLLBACK_DIR}"
mv "${STAGED_REPO}" "${REPO_DIR}"
SWAPPED=1

info "Restoring runtime-owned state into the new checkout"
restore_preserved_paths "${PRESERVE_ROOT}" "${REPO_DIR}"

# Seed the operator env into the sanctioned out-of-mount location; a repo-root
# .env would trip the secrets-visible-in-workspace / operator-secret-containment
# doctor checks on fresh upgrades (skillbox-4c9s).
OPERATOR_ENV_DIR="${REPO_DIR}/.skillbox-state/operator"
if [[ ! -f "${OPERATOR_ENV_DIR}/.env" && ! -f "${REPO_DIR}/.env" && -f "${REPO_DIR}/.env.example" ]]; then
  mkdir -p "${OPERATOR_ENV_DIR}"
  cp "${REPO_DIR}/.env.example" "${OPERATOR_ENV_DIR}/.env"
  chmod 600 "${OPERATOR_ENV_DIR}/.env"
fi

info "Building upgraded workspace image"
repo_lifecycle_target "${REPO_DIR}" build

info "Starting upgraded workspace"
repo_lifecycle_target "${REPO_DIR}" up

if [[ -f "${REPO_DIR}/VERSION.txt" ]]; then
  AFTER_VERSION="$(tr -d '\n' <"${REPO_DIR}/VERSION.txt")"
fi

# Converge DCG against the NEW release's policy, then re-validate before the
# upgrade may call itself successful. A converge that "succeeded" but left the
# guard unhealthy is the exact false-green this gate exists to catch, so verify
# is a separate step and not an assumption.
info "Converging DCG for the upgraded release"
run_dcg_action apply

info "Re-validating DCG before declaring success"
run_dcg_action verify

ACCEPTANCE_CMD=(python3 ".env-manager/manage.py" "acceptance" "${CLIENT_ID}" "--format" "json")
for profile in "${PROFILE_ARGS[@]}"; do
  ACCEPTANCE_CMD+=("--profile" "${profile}")
done

info "Running acceptance gate for ${CLIENT_ID}"
(cd "${REPO_DIR}" && "${ACCEPTANCE_CMD[@]}" >/dev/null)

write_upgrade_receipt

rm -rf "${ROLLBACK_DIR}"
SUCCESS=1
info "Upgrade complete"
