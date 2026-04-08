#!/usr/bin/env bash
# Install with:
#   curl -fsSL https://raw.githubusercontent.com/build000r/skillbox/main/install.sh | bash -s -- --client personal
set -euo pipefail
shopt -s lastpipe 2>/dev/null || true
umask 022

PROJECT_NAME="skillbox"
PROJECT_LABEL="skillbox installer"
PROJECT_DESCRIPTION="Source-distributed bootstrap for first-box"
DEFAULT_REPO_URL="https://github.com/build000r/skillbox.git"
DEFAULT_REF_FALLBACK="main"
MIN_DISK_KB=102400

QUIET=0
NO_GUM=0
FORCE=0
DRY_RUN=0
VERIFY=0
RUN_BUILD=1
RUN_UP=1
RUN_FIRST_BOX=1
RUN_BOOTSTRAP_HOST=0
RUN_TAILSCALE=0

CLIENT_ID="personal"
REPO_DIR=""
PRIVATE_PATH=""
SOURCE_DIR=""
SOURCE_REPO="${DEFAULT_REPO_URL}"
OFFLINE_TARBALL=""
SOURCE_SHA256=""
REF=""
BLUEPRINT=""
TAILSCALE_AUTHKEY="${TAILSCALE_AUTHKEY:-}"
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-skillbox-dev}"

HAS_GUM=0
if command -v gum >/dev/null 2>&1 && [ -t 1 ]; then
  HAS_GUM=1
fi

PROXY_ARGS=()
PROFILE_ARGS=()
FIRST_BOX_SET_ARGS=()

STATUS_SOURCE="pending"
STATUS_ENV="pending"
STATUS_BOOTSTRAP="skipped"
STATUS_TAILSCALE="skipped"
STATUS_FIRST_BOX="pending"
STATUS_BUILD="skipped"
STATUS_UP="skipped"
STATUS_VERIFY="skipped"

FIRST_BOX_OUTPUT_DIR=""
FIRST_BOX_PRIVATE_REPO=""
LOCK_DIR=""
TEMP_DIR=""
SCRIPT_SOURCE="${BASH_SOURCE[0]:-${0:-}}"
SCRIPT_PATH=""
SCRIPT_DIR=""
if [[ -n "${SCRIPT_SOURCE}" && "${SCRIPT_SOURCE}" != "bash" && "${SCRIPT_SOURCE}" != "-" ]]; then
  SCRIPT_PATH="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${SCRIPT_SOURCE}")"
  SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
fi
RUNNING_FROM_CHECKOUT=0
if [[ -n "${SCRIPT_DIR}" && -f "${SCRIPT_DIR}/.env-manager/manage.py" && -f "${SCRIPT_DIR}/README.md" ]]; then
  RUNNING_FROM_CHECKOUT=1
fi

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Core options:
  --client <id>            Client slug to prepare. Defaults to personal.
  --repo-dir <path>        Checkout/install directory. Defaults to the current
                           repo when run from a checkout, otherwise ~/skillbox.
  --private-path <path>    Private overlay repo path. Defaults to ../skillbox-config
                           relative to --repo-dir.
  --profile <name>         Runtime profile to activate during first-box.
                           Can be repeated.
  --blueprint <name>       Client blueprint to apply when scaffolding.
  --set KEY=VALUE          Blueprint variable assignment. Can be repeated.

Source acquisition:
  --source-dir <path>      Copy from an existing local checkout.
  --source-repo <url>      Git source repo to clone. Defaults to the official repo.
  --ref <ref>              Git branch or tag to clone/download. Defaults to the
                           latest release tag when resolvable, otherwise main.
  --offline <tarball>      Extract from a local source tarball instead of cloning.
  --sha256 <hex>           Expected SHA256 for --offline tarball or downloaded tarball.

Lifecycle:
  --skip-first-box         Do not run first-box after acquiring the source.
  --skip-build             Do not run make build after first-box.
  --skip-up                Do not run make up after first-box.
  --verify                 Run post-install runtime verification commands.
  --bootstrap-host         Run scripts/01-bootstrap-do.sh before build/up.
  --tailscale              Run scripts/02-install-tailscale.sh after bootstrap.

Behavior:
  --dry-run                Print the planned actions without writing anything.
  --force                  Replace an existing checkout target or scaffold files.
  --quiet                  Reduce non-error output.
  --no-gum                 Force plain ANSI/text output even when gum is available.
  -h, --help               Show this help.

Examples:
  bash install.sh --client personal --skip-build --skip-up
  bash install.sh --source-dir . --repo-dir /tmp/skillbox --private-path /tmp/skillbox-config --skip-build --skip-up
  bash install.sh --offline ./skillbox.tar.gz --repo-dir ~/skillbox --verify
EOF
}

setup_proxy() {
  PROXY_ARGS=()
  if [[ -n "${HTTPS_PROXY:-}" ]]; then
    PROXY_ARGS=(--proxy "${HTTPS_PROXY}")
  elif [[ -n "${HTTP_PROXY:-}" ]]; then
    PROXY_ARGS=(--proxy "${HTTP_PROXY}")
  fi
}

strip_ansi() {
  sed -E $'s/\x1B\\[[0-9;]*[[:alpha:]]//g'
}

draw_box() {
  local color="$1"
  shift
  local lines=("$@")
  local width=0
  local line=""
  local clean=""
  local border=""

  for line in "${lines[@]}"; do
    clean="$(printf '%s' "${line}" | strip_ansi)"
    if [[ ${#clean} -gt ${width} ]]; then
      width=${#clean}
    fi
  done

  border="+"
  while [[ ${#border} -lt $((width + 4)) ]]; do
    border="${border}-"
  done
  border="${border}+"

  if [[ -n "${color}" ]]; then
    printf '%b%s%b\n' "${color}" "${border}" '\033[0m'
  else
    printf '%s\n' "${border}"
  fi

  for line in "${lines[@]}"; do
    clean="$(printf '%s' "${line}" | strip_ansi)"
    printf '%s %s' "| " "${line}"
    while [[ ${#clean} -lt ${width} ]]; do
      printf ' '
      clean="${clean} "
    done
    printf ' |\n'
  done

  if [[ -n "${color}" ]]; then
    printf '%b%s%b\n' "${color}" "${border}" '\033[0m'
  else
    printf '%s\n' "${border}"
  fi
}

info() {
  [[ "${QUIET}" -eq 1 ]] && return 0
  if [[ "${HAS_GUM}" -eq 1 && "${NO_GUM}" -eq 0 ]]; then
    gum style --foreground 39 "-> $*"
  else
    printf '\033[0;34m->\033[0m %s\n' "$*"
  fi
}

ok() {
  [[ "${QUIET}" -eq 1 ]] && return 0
  if [[ "${HAS_GUM}" -eq 1 && "${NO_GUM}" -eq 0 ]]; then
    gum style --foreground 42 "OK $*"
  else
    printf '\033[0;32mOK\033[0m %s\n' "$*"
  fi
}

warn() {
  [[ "${QUIET}" -eq 1 ]] && return 0
  if [[ "${HAS_GUM}" -eq 1 && "${NO_GUM}" -eq 0 ]]; then
    gum style --foreground 214 "WARN $*"
  else
    printf '\033[1;33mWARN\033[0m %s\n' "$*"
  fi
}

err() {
  if [[ "${HAS_GUM}" -eq 1 && "${NO_GUM}" -eq 0 ]]; then
    gum style --foreground 196 "ERR $*"
  else
    printf '\033[0;31mERR\033[0m %s\n' "$*" >&2
  fi
}

run_with_spinner() {
  local title="$1"
  shift
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    info "dry-run: ${title}"
    return 0
  fi
  if [[ "${HAS_GUM}" -eq 1 && "${NO_GUM}" -eq 0 && "${QUIET}" -eq 0 ]]; then
    gum spin --spinner dot --title "${title}" -- "$@"
  else
    info "${title}"
    "$@"
  fi
}

resolve_abs_path() {
  python3 - "$1" <<'PY'
import os
import sys

value = sys.argv[1]
print(os.path.realpath(os.path.expanduser(value)))
PY
}

json_get() {
  local json_file="$1"
  local path="$2"
  python3 - "$json_file" "$path" <<'PY'
import json
import sys
from pathlib import Path

node = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for key in [p for p in sys.argv[2].split(".") if p]:
    if not isinstance(node, dict) or key not in node:
        node = ""
        break
    node = node[key]
print(node if node is not None else "")
PY
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    err "Missing required command: $1"
    exit 1
  fi
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

sha256_file() {
  local path="$1"
  if have_cmd sha256sum; then
    sha256sum "${path}" | awk '{print $1}'
  else
    shasum -a 256 "${path}" | awk '{print $1}'
  fi
}

verify_checksum() {
  local path="$1"
  local expected="$2"
  local actual=""
  if [[ -z "${expected}" ]]; then
    warn "No SHA256 provided for ${path}; skipping checksum verification."
    return 0
  fi
  actual="$(sha256_file "${path}")"
  if [[ "${actual}" != "${expected}" ]]; then
    err "Checksum mismatch for ${path}"
    err "Expected: ${expected}"
    err "Actual:   ${actual}"
    exit 1
  fi
}

maybe_verify_sigstore_bundle() {
  local path="$1"
  local bundle_path="${path}.sigstore.json"
  if [[ ! -f "${bundle_path}" ]]; then
    return 0
  fi
  if ! have_cmd cosign; then
    warn "Found ${bundle_path}, but cosign is not installed; skipping sigstore verification."
    return 0
  fi
  warn "Sigstore bundle found for ${path}, but identity policy is not configured for source installs yet."
}

check_disk_space() {
  local target_parent="$1"
  local available=""
  available="$(df -Pk "${target_parent}" | awk 'NR==2 {print $4}')"
  if [[ -z "${available}" ]]; then
    err "Could not determine free disk space for ${target_parent}"
    exit 1
  fi
  if [[ "${available}" -lt "${MIN_DISK_KB}" ]]; then
    err "Need at least $((MIN_DISK_KB / 1024))MB free in ${target_parent}; found $((available / 1024))MB."
    exit 1
  fi
}

check_write_permissions() {
  local target_parent="$1"
  mkdir -p "${target_parent}"
  if [[ ! -w "${target_parent}" ]]; then
    err "Install target is not writable: ${target_parent}"
    exit 1
  fi
}

check_network() {
  if [[ -n "${SOURCE_DIR}" || -n "${OFFLINE_TARBALL}" ]]; then
    return 0
  fi
  if have_cmd curl; then
    if ! curl -fsSL --connect-timeout 5 "${PROXY_ARGS[@]}" https://github.com >/dev/null 2>&1; then
      err "Network check failed for GitHub."
      exit 1
    fi
  fi
}

directory_has_entries() {
  local path="$1"
  [[ -d "${path}" ]] || return 1
  find "${path}" -mindepth 1 -maxdepth 1 | grep -q .
}

ensure_checkout_target_ready() {
  local target="$1"
  if [[ -d "${target}" ]] && directory_has_entries "${target}"; then
    if [[ "${FORCE}" -eq 1 ]]; then
      warn "Removing existing checkout target: ${target}"
      rm -rf "${target}"
    else
      err "Checkout target already exists and is not empty: ${target}"
      err "Use --force to replace it."
      exit 1
    fi
  fi
}

cleanup() {
  if [[ -n "${TEMP_DIR}" && -d "${TEMP_DIR}" ]]; then
    rm -rf "${TEMP_DIR}"
  fi
  if [[ -n "${LOCK_DIR}" && -d "${LOCK_DIR}" ]]; then
    rmdir "${LOCK_DIR}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

acquire_lock() {
  local base="${TMPDIR:-/tmp}"
  LOCK_DIR="${base}/skillbox-install.lock"
  if mkdir "${LOCK_DIR}" 2>/dev/null; then
    return 0
  fi
  err "Another skillbox install appears to be running (${LOCK_DIR})."
  exit 1
}

detect_platform() {
  local os=""
  local arch=""
  os="$(uname -s | tr 'A-Z' 'a-z')"
  arch="$(uname -m)"
  case "${arch}" in
    x86_64|amd64) arch="x86_64" ;;
    arm64|aarch64) arch="aarch64" ;;
  esac
  if [[ "${os}" == "linux" ]] && grep -qi microsoft /proc/version 2>/dev/null; then
    warn "WSL detected. Host bootstrap and Docker behavior may need additional adjustment."
  fi
  ok "Detected platform ${os}/${arch}"
}

resolve_default_ref() {
  if [[ -n "${REF}" ]]; then
    return 0
  fi
  if have_cmd curl; then
    local resolved=""
    resolved="$(curl -fsSL "${PROXY_ARGS[@]}" "https://api.github.com/repos/build000r/skillbox/releases/latest" 2>/dev/null | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1 || true)"
    if [[ -n "${resolved}" ]]; then
      REF="${resolved}"
      return 0
    fi
  fi
  REF="${DEFAULT_REF_FALLBACK}"
}

download_source_tarball() {
  local dest="$1"
  local url=""
  local ref_kind="tags"
  resolve_default_ref
  if [[ "${REF}" == "${DEFAULT_REF_FALLBACK}" ]]; then
    ref_kind="heads"
  fi
  url="https://github.com/build000r/skillbox/archive/refs/${ref_kind}/${REF}.tar.gz"
  if ! curl -fsSL "${PROXY_ARGS[@]}" "${url}" -o "${dest}"; then
    url="https://github.com/build000r/skillbox/archive/refs/heads/${DEFAULT_REF_FALLBACK}.tar.gz"
    curl -fsSL "${PROXY_ARGS[@]}" "${url}" -o "${dest}"
  fi
}

copy_checkout() {
  local src="$1"
  local dest="$2"
  local rel=""
  local always_copy=""

  if [[ "${src}" == "${dest}" ]]; then
    STATUS_SOURCE="reused"
    return 0
  fi

  ensure_checkout_target_ready "${dest}"
  mkdir -p "${dest}"

  if git -C "${src}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    while IFS= read -r -d '' rel; do
      mkdir -p "${dest}/$(dirname "${rel}")"
      cp -pR "${src}/${rel}" "${dest}/${rel}"
    done < <(git -C "${src}" ls-files -z --cached --modified --others --exclude-standard)
    for always_copy in ".mcp.json"; do
      if [[ -f "${src}/${always_copy}" ]]; then
        mkdir -p "${dest}/$(dirname "${always_copy}")"
        cp -p "${src}/${always_copy}" "${dest}/${always_copy}"
      fi
    done
  else
    rsync -a \
      --exclude '.git/' \
      --exclude '.cache/' \
      --exclude '.pytest_cache/' \
      --exclude '.venv/' \
      --exclude '__pycache__/' \
      --exclude '*.pyc' \
      --exclude '.env' \
      --exclude '.coverage' \
      --exclude '.coverage.*' \
      --exclude 'coverage.xml' \
      --exclude 'sand/' \
      --exclude 'data/' \
      --exclude 'home/.local/' \
      --exclude 'workspace/.focus.json' \
      --exclude 'workspace/.compose-overrides/' \
      --exclude 'workspace/clients/*/context.yaml' \
      --exclude 'workspace/clients/*/skills.lock.json' \
      "${src}/" "${dest}/"
  fi
  STATUS_SOURCE="copied"
}

extract_tarball_checkout() {
  local tarball="$1"
  local dest="$2"
  local extract_root=""

  ensure_checkout_target_ready "${dest}"
  TEMP_DIR="$(mktemp -d)"
  mkdir -p "${TEMP_DIR}/extract"
  tar -xzf "${tarball}" -C "${TEMP_DIR}/extract"
  extract_root="$(find "${TEMP_DIR}/extract" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [[ -z "${extract_root}" ]]; then
    err "Could not find extracted source root in ${tarball}"
    exit 1
  fi
  mkdir -p "${dest}"
  rsync -a "${extract_root}/" "${dest}/"
  STATUS_SOURCE="extracted"
}

clone_checkout() {
  local repo_url="$1"
  local dest="$2"

  ensure_checkout_target_ready "${dest}"
  resolve_default_ref
  if git clone --branch "${REF}" --depth 1 "${repo_url}" "${dest}" >/dev/null 2>&1; then
    STATUS_SOURCE="cloned"
    return 0
  fi
  git clone "${repo_url}" "${dest}" >/dev/null 2>&1
  (
    cd "${dest}"
    git checkout "${REF}" >/dev/null 2>&1 || git checkout "${DEFAULT_REF_FALLBACK}" >/dev/null 2>&1
  )
  STATUS_SOURCE="cloned"
}

resolve_repo_and_private_paths() {
  local default_repo_dir=""
  local repo_parent=""

  if [[ "${RUNNING_FROM_CHECKOUT}" -eq 1 ]]; then
    default_repo_dir="${SCRIPT_DIR}"
  else
    default_repo_dir="${HOME}/skillbox"
  fi

  if [[ -z "${REPO_DIR}" ]]; then
    REPO_DIR="${default_repo_dir}"
  fi
  REPO_DIR="$(resolve_abs_path "${REPO_DIR}")"

  repo_parent="$(dirname "${REPO_DIR}")"
  if [[ -z "${PRIVATE_PATH}" ]]; then
    PRIVATE_PATH="${repo_parent}/skillbox-config"
  fi
  PRIVATE_PATH="$(resolve_abs_path "${PRIVATE_PATH}")"
}

preflight_checks() {
  local repo_parent=""
  repo_parent="$(dirname "${REPO_DIR}")"
  info "Running preflight checks"
  require_cmd python3
  require_cmd tar
  if [[ -n "${SOURCE_DIR}" || ( "${RUNNING_FROM_CHECKOUT}" -eq 1 && "${REPO_DIR}" != "${SCRIPT_DIR}" ) ]]; then
    require_cmd rsync
  fi
  if [[ -z "${OFFLINE_TARBALL}" ]]; then
    if have_cmd git; then
      :
    elif have_cmd curl; then
      :
    else
      err "Need either git or curl to acquire source."
      exit 1
    fi
  fi
  if [[ -n "${SOURCE_SHA256}" ]]; then
    if ! have_cmd shasum && ! have_cmd sha256sum; then
      err "Need shasum or sha256sum to verify SHA256 digests."
      exit 1
    fi
  fi
  if [[ "${RUN_BUILD}" -eq 1 || "${RUN_UP}" -eq 1 ]]; then
    require_cmd docker
  fi
  check_disk_space "${repo_parent}"
  check_write_permissions "${repo_parent}"
  check_write_permissions "$(dirname "${PRIVATE_PATH}")"
  check_network
}

hydrate_env() {
  local target_repo="$1"
  local env_file="${target_repo}/.env"
  local env_example="${target_repo}/.env.example"

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    STATUS_ENV="planned"
    return 0
  fi
  if [[ ! -f "${env_example}" ]]; then
    err "Missing ${env_example}"
    exit 1
  fi
  if [[ -f "${env_file}" ]]; then
    STATUS_ENV="kept"
    return 0
  fi
  cp "${env_example}" "${env_file}"
  STATUS_ENV="created"
}

ensure_local_state_layout() {
  local target_repo="$1"
  local env_file="${target_repo}/.env"
  local dir_path=""

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    info "dry-run: prepare local state layout under ${target_repo}"
    return 0
  fi
  if [[ ! -f "${env_file}" ]]; then
    return 0
  fi

  while IFS= read -r dir_path; do
    [[ -n "${dir_path}" ]] || continue
    mkdir -p "${dir_path}"
  done < <(python3 - "${target_repo}" "${env_file}" <<'PY'
import os
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
env_file = Path(sys.argv[2])
values = {}
for raw_line in env_file.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip()

state_root_raw = values.get("SKILLBOX_STATE_ROOT", "./.skillbox-state")
monoserver_raw = values.get("SKILLBOX_MONOSERVER_HOST_ROOT", "${SKILLBOX_STATE_ROOT}/monoserver")

def resolve_host_path(raw: str) -> Path:
    expanded = raw.replace("${SKILLBOX_STATE_ROOT}", state_root_raw)
    path = Path(os.path.expanduser(expanded))
    if not path.is_absolute():
        path = (repo / path).resolve()
    else:
        path = path.resolve()
    return path

state_root = resolve_host_path(state_root_raw)
monoserver_root = resolve_host_path(monoserver_raw)

dirs = [
    state_root,
    state_root / "home" / ".claude",
    state_root / "home" / ".codex",
    state_root / "home" / ".local",
    state_root / "logs",
    monoserver_root,
]

seen = set()
for path in dirs:
    text = str(path)
    if text in seen:
        continue
    seen.add(text)
    print(text)
PY
  )
}

run_host_bootstrap() {
  local target_repo="$1"
  local bootstrap_script="${target_repo}/scripts/01-bootstrap-do.sh"
  if [[ "${RUN_BOOTSTRAP_HOST}" -ne 1 ]]; then
    return 0
  fi
  STATUS_BOOTSTRAP="pending"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    STATUS_BOOTSTRAP="planned"
    return 0
  fi
  if [[ "$(uname -s)" != "Linux" ]]; then
    err "--bootstrap-host is only supported on Linux hosts."
    exit 1
  fi
  if [[ "${EUID}" -eq 0 ]]; then
    run_with_spinner "Running host bootstrap" bash "${bootstrap_script}"
  elif have_cmd sudo; then
    run_with_spinner "Running host bootstrap" sudo bash "${bootstrap_script}"
  else
    err "sudo is required for --bootstrap-host."
    exit 1
  fi
  STATUS_BOOTSTRAP="ok"
}

run_tailscale_setup() {
  local target_repo="$1"
  local tailscale_script="${target_repo}/scripts/02-install-tailscale.sh"
  if [[ "${RUN_TAILSCALE}" -ne 1 ]]; then
    return 0
  fi
  STATUS_TAILSCALE="pending"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    STATUS_TAILSCALE="planned"
    return 0
  fi
  if [[ "$(uname -s)" != "Linux" ]]; then
    err "--tailscale is only supported on Linux hosts."
    exit 1
  fi
  if [[ "${EUID}" -eq 0 ]]; then
    TAILSCALE_AUTHKEY="${TAILSCALE_AUTHKEY}" TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME}" bash "${tailscale_script}"
  elif have_cmd sudo; then
    run_with_spinner "Running Tailscale setup" sudo env TAILSCALE_AUTHKEY="${TAILSCALE_AUTHKEY}" TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME}" bash "${tailscale_script}"
  else
    err "sudo is required for --tailscale."
    exit 1
  fi
  STATUS_TAILSCALE="ok"
}

run_first_box() {
  local target_repo="$1"
  local output_file="$2"
  local cmd=()
  local profile=""
  local assignment=""

  if [[ "${RUN_FIRST_BOX}" -ne 1 ]]; then
    STATUS_FIRST_BOX="skipped"
    return 0
  fi

  STATUS_FIRST_BOX="pending"
  FIRST_BOX_OUTPUT_DIR="${target_repo}/sand/${CLIENT_ID}"
  FIRST_BOX_PRIVATE_REPO="${PRIVATE_PATH}"

  cmd=(python3 ".env-manager/manage.py" "first-box" "${CLIENT_ID}" "--private-path" "${PRIVATE_PATH}" "--format" "json")
  for profile in "${PROFILE_ARGS[@]}"; do
    cmd+=("--profile" "${profile}")
  done
  if [[ -n "${BLUEPRINT}" ]]; then
    cmd+=("--blueprint" "${BLUEPRINT}")
  fi
  for assignment in "${FIRST_BOX_SET_ARGS[@]}"; do
    cmd+=("--set" "${assignment}")
  done
  if [[ "${FORCE}" -eq 1 ]]; then
    cmd+=("--force")
  fi

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    info "dry-run: (cd ${target_repo} && ${cmd[*]})"
    STATUS_FIRST_BOX="planned"
    return 0
  fi

  if ! (
    cd "${target_repo}"
    "${cmd[@]}" >"${output_file}"
  ); then
    STATUS_FIRST_BOX="fail"
    err "first-box failed"
    cat "${output_file}" >&2 || true
    exit 1
  fi
  STATUS_FIRST_BOX="ok"
  FIRST_BOX_OUTPUT_DIR="$(json_get "${output_file}" "output_dir" || printf '%s' "${FIRST_BOX_OUTPUT_DIR}")"
  FIRST_BOX_PRIVATE_REPO="$(json_get "${output_file}" "private_repo.target_dir" || printf '%s' "${FIRST_BOX_PRIVATE_REPO}")"
}

run_make_target() {
  local target_repo="$1"
  local target_name="$2"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    info "dry-run: (cd ${target_repo} && make ${target_name})"
    return 0
  fi
  (
    cd "${target_repo}"
    make "${target_name}"
  )
}

run_verify() {
  local target_repo="$1"
  local profile=""
  local cmd=()

  if [[ "${VERIFY}" -ne 1 ]]; then
    return 0
  fi
  if [[ "${RUN_FIRST_BOX}" -ne 1 ]]; then
    STATUS_VERIFY="skipped"
    return 0
  fi
  STATUS_VERIFY="pending"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    STATUS_VERIFY="planned"
    return 0
  fi

  cmd=(python3 ".env-manager/manage.py" "doctor" "--client" "${CLIENT_ID}" "--format" "json")
  for profile in "${PROFILE_ARGS[@]}"; do
    cmd+=("--profile" "${profile}")
  done
  (
    cd "${target_repo}"
    "${cmd[@]}" >/dev/null
  )

  cmd=(python3 ".env-manager/manage.py" "status" "--client" "${CLIENT_ID}" "--format" "json")
  for profile in "${PROFILE_ARGS[@]}"; do
    cmd+=("--profile" "${profile}")
  done
  (
    cd "${target_repo}"
    "${cmd[@]}" >/dev/null
  )
  STATUS_VERIFY="ok"
}

print_header() {
  if [[ "${HAS_GUM}" -eq 1 && "${NO_GUM}" -eq 0 ]]; then
    gum style \
      --border normal \
      --border-foreground 39 \
      --padding "0 1" \
      --margin "1 0" \
      "$(gum style --foreground 42 --bold "${PROJECT_LABEL}")" \
      "$(gum style --foreground 245 "${PROJECT_DESCRIPTION}")"
  else
    draw_box '\033[0;36m' "${PROJECT_LABEL}" "${PROJECT_DESCRIPTION}"
  fi
}

print_summary() {
  local lines=()
  lines+=("repo_dir: ${REPO_DIR}")
  lines+=("private_repo: ${FIRST_BOX_PRIVATE_REPO:-${PRIVATE_PATH}}")
  lines+=("client: ${CLIENT_ID}")
  lines+=("open_surface: ${FIRST_BOX_OUTPUT_DIR:-${REPO_DIR}/sand/${CLIENT_ID}}")
  lines+=("source: ${STATUS_SOURCE}")
  lines+=("env: ${STATUS_ENV}")
  lines+=("bootstrap_host: ${STATUS_BOOTSTRAP}")
  lines+=("tailscale: ${STATUS_TAILSCALE}")
  lines+=("first_box: ${STATUS_FIRST_BOX}")
  lines+=("build: ${STATUS_BUILD}")
  lines+=("up: ${STATUS_UP}")
  lines+=("verify: ${STATUS_VERIFY}")
  lines+=("private source of truth lives under ${FIRST_BOX_PRIVATE_REPO:-${PRIVATE_PATH}}")
  lines+=("sand/${CLIENT_ID} is generated and can be rebuilt")
  lines+=("uninstall: rm -rf ${REPO_DIR} ${FIRST_BOX_PRIVATE_REPO:-${PRIVATE_PATH}}")
  draw_box '\033[0;32m' "${lines[@]}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --client)
      CLIENT_ID="$2"
      shift 2
      ;;
    --repo-dir)
      REPO_DIR="$2"
      shift 2
      ;;
    --private-path)
      PRIVATE_PATH="$2"
      shift 2
      ;;
    --profile)
      PROFILE_ARGS+=("$2")
      shift 2
      ;;
    --blueprint)
      BLUEPRINT="$2"
      shift 2
      ;;
    --set)
      FIRST_BOX_SET_ARGS+=("$2")
      shift 2
      ;;
    --source-dir)
      SOURCE_DIR="$2"
      shift 2
      ;;
    --source-repo)
      SOURCE_REPO="$2"
      shift 2
      ;;
    --ref)
      REF="$2"
      shift 2
      ;;
    --offline)
      OFFLINE_TARBALL="$2"
      shift 2
      ;;
    --sha256)
      SOURCE_SHA256="$2"
      shift 2
      ;;
    --skip-build)
      RUN_BUILD=0
      shift
      ;;
    --skip-first-box)
      RUN_FIRST_BOX=0
      shift
      ;;
    --skip-up)
      RUN_UP=0
      shift
      ;;
    --verify)
      VERIFY=1
      shift
      ;;
    --bootstrap-host)
      RUN_BOOTSTRAP_HOST=1
      shift
      ;;
    --tailscale)
      RUN_TAILSCALE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --quiet)
      QUIET=1
      shift
      ;;
    --no-gum)
      NO_GUM=1
      shift
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

print_header
setup_proxy
resolve_repo_and_private_paths
detect_platform
preflight_checks
acquire_lock

if [[ -n "${SOURCE_DIR}" ]]; then
  SOURCE_DIR="$(resolve_abs_path "${SOURCE_DIR}")"
  info "Acquiring source from local checkout ${SOURCE_DIR}"
  if [[ ! -d "${SOURCE_DIR}" ]]; then
    err "Local source directory not found: ${SOURCE_DIR}"
    exit 1
  fi
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    STATUS_SOURCE="planned"
  else
    copy_checkout "${SOURCE_DIR}" "${REPO_DIR}"
  fi
elif [[ -n "${OFFLINE_TARBALL}" ]]; then
  OFFLINE_TARBALL="$(resolve_abs_path "${OFFLINE_TARBALL}")"
  info "Acquiring source from offline tarball ${OFFLINE_TARBALL}"
  if [[ ! -f "${OFFLINE_TARBALL}" ]]; then
    err "Offline tarball not found: ${OFFLINE_TARBALL}"
    exit 1
  fi
  verify_checksum "${OFFLINE_TARBALL}" "${SOURCE_SHA256}"
  maybe_verify_sigstore_bundle "${OFFLINE_TARBALL}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    STATUS_SOURCE="planned"
  else
    extract_tarball_checkout "${OFFLINE_TARBALL}" "${REPO_DIR}"
  fi
elif [[ "${RUNNING_FROM_CHECKOUT}" -eq 1 && "${REPO_DIR}" == "${SCRIPT_DIR}" ]]; then
  info "Reusing current checkout ${SCRIPT_DIR}"
  STATUS_SOURCE="reused"
elif have_cmd git; then
  info "Cloning ${SOURCE_REPO} into ${REPO_DIR}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    STATUS_SOURCE="planned"
  else
    clone_checkout "${SOURCE_REPO}" "${REPO_DIR}"
  fi
else
  require_cmd curl
  TEMP_DIR="$(mktemp -d)"
  info "Downloading source tarball for ${PROJECT_NAME}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    STATUS_SOURCE="planned"
  else
    download_source_tarball "${TEMP_DIR}/skillbox.tar.gz"
    verify_checksum "${TEMP_DIR}/skillbox.tar.gz" "${SOURCE_SHA256}"
    maybe_verify_sigstore_bundle "${TEMP_DIR}/skillbox.tar.gz"
    extract_tarball_checkout "${TEMP_DIR}/skillbox.tar.gz" "${REPO_DIR}"
  fi
fi

hydrate_env "${REPO_DIR}"
ensure_local_state_layout "${REPO_DIR}"
run_host_bootstrap "${REPO_DIR}"
run_tailscale_setup "${REPO_DIR}"

FIRST_BOX_JSON=""
if [[ "${DRY_RUN}" -eq 0 ]]; then
  FIRST_BOX_JSON="$(mktemp)"
fi
run_first_box "${REPO_DIR}" "${FIRST_BOX_JSON:-/dev/null}"

if [[ "${RUN_BUILD}" -eq 1 ]]; then
  STATUS_BUILD="pending"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    STATUS_BUILD="planned"
  else
    run_with_spinner "Building workspace image" bash -lc "cd \"${REPO_DIR}\" && make build"
    STATUS_BUILD="ok"
  fi
fi

if [[ "${RUN_UP}" -eq 1 ]]; then
  STATUS_UP="pending"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    STATUS_UP="planned"
  else
    run_with_spinner "Starting workspace container" bash -lc "cd \"${REPO_DIR}\" && make up"
    STATUS_UP="ok"
  fi
fi

run_verify "${REPO_DIR}"

if [[ "${STATUS_VERIFY}" == "pending" ]]; then
  STATUS_VERIFY="ok"
fi

print_summary
