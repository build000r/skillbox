#!/usr/bin/env bash
set -euo pipefail

ARCHIVE=""
ARCHIVE_SHA256=""
REPO_DIR=""
CLIENT_ID=""
ROLLBACK_DIR=""
TEMP_DIR=""
PRESERVE_ROOT=""
SWAPPED=0
SUCCESS=0
STOPPED_OLD=0
PROFILE_ARGS=()

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

usage() {
  cat <<'EOF'
Usage: 06-upgrade-release.sh --archive <path> --sha256 <hex> --repo-dir <path> --client <id> [--profile <name>]
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

rollback() {
  local status="$1"

  if [[ "${SUCCESS}" -eq 1 ]]; then
    if [[ -n "${TEMP_DIR}" && -d "${TEMP_DIR}" ]]; then
      rm -rf "${TEMP_DIR}"
    fi
    return
  fi

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

TEMP_DIR="$(mktemp -d)"
PRESERVE_ROOT="${TEMP_DIR}/preserve"
mkdir -p "${PRESERVE_ROOT}" "${TEMP_DIR}/extract"

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

if [[ ! -f "${REPO_DIR}/.env" && -f "${REPO_DIR}/.env.example" ]]; then
  cp "${REPO_DIR}/.env.example" "${REPO_DIR}/.env"
fi

info "Building upgraded workspace image"
repo_lifecycle_target "${REPO_DIR}" build

info "Starting upgraded workspace"
repo_lifecycle_target "${REPO_DIR}" up

ACCEPTANCE_CMD=(python3 ".env-manager/manage.py" "acceptance" "${CLIENT_ID}" "--format" "json")
for profile in "${PROFILE_ARGS[@]}"; do
  ACCEPTANCE_CMD+=("--profile" "${profile}")
done

info "Running acceptance gate for ${CLIENT_ID}"
(cd "${REPO_DIR}" && "${ACCEPTANCE_CMD[@]}" >/dev/null)

rm -rf "${ROLLBACK_DIR}"
SUCCESS=1
info "Upgrade complete"
