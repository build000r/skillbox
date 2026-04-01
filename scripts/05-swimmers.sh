#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.swimmers.yml)
WORKSPACE_SERVICE="${SKILLBOX_WORKSPACE_SERVICE:-workspace}"
INSIDE_FLAG="--inside"

usage() {
  cat <<'EOF'
usage: scripts/05-swimmers.sh [install|start|stop|restart|status|logs]

Host mode:
  Ensures the workspace container is started with the swimmers compose overlay,
  then delegates the requested action into the workspace container.

Inside mode:
  Internal helper used from inside the workspace container.
EOF
}

compose_cmd() {
  (cd "${ROOT_DIR}" && docker compose "${COMPOSE_FILES[@]}" "$@")
}

workspace_running() {
  local services
  services="$(compose_cmd ps --status running --services 2>/dev/null || true)"
  grep -qx "${WORKSPACE_SERVICE}" <<<"${services}"
}

ensure_workspace_running() {
  printf 'Ensuring workspace is running with swimmers overlay\n'
  compose_cmd up -d "${WORKSPACE_SERVICE}"
}

run_inside() {
  local command="${1:?missing command}"
  shift || true

  if [[ "${command}" == "logs" ]]; then
    compose_cmd exec "${WORKSPACE_SERVICE}" /workspace/scripts/05-swimmers.sh "${INSIDE_FLAG}" "${command}" "$@"
    return
  fi

  compose_cmd exec -T "${WORKSPACE_SERVICE}" /workspace/scripts/05-swimmers.sh "${INSIDE_FLAG}" "${command}" "$@"
}

inside_main() {
  local command="${1:-}"
  if [[ -z "${command}" ]]; then
    usage
    exit 1
  fi

  local workspace_root="${SKILLBOX_WORKSPACE_ROOT:-/workspace}"
  local log_root="${SKILLBOX_LOG_ROOT:-${workspace_root}/logs}"
  local home_root="${SKILLBOX_HOME_ROOT:-/home/sandbox}"
  local monoserver_root="${SKILLBOX_MONOSERVER_ROOT:-/monoserver}"
  local swimmers_repo="${SKILLBOX_SWIMMERS_REPO:-${monoserver_root}/swimmers}"
  local swimmers_port="${SKILLBOX_SWIMMERS_PORT:-3210}"
  local swimmers_publish_host="${SKILLBOX_SWIMMERS_PUBLISH_HOST:-127.0.0.1}"
  local swimmers_install_dir="${SKILLBOX_SWIMMERS_INSTALL_DIR:-${home_root}/.local/bin}"
  local swimmers_bin="${SKILLBOX_SWIMMERS_BIN:-${swimmers_install_dir}/swimmers}"
  local swimmers_download_url="${SKILLBOX_SWIMMERS_DOWNLOAD_URL:-}"
  local swimmers_download_sha256="${SKILLBOX_SWIMMERS_DOWNLOAD_SHA256:-}"
  local swimmers_auth_mode="${SKILLBOX_SWIMMERS_AUTH_MODE:-}"
  local swimmers_auth_token="${SKILLBOX_SWIMMERS_AUTH_TOKEN:-}"
  local swimmers_observer_token="${SKILLBOX_SWIMMERS_OBSERVER_TOKEN:-}"
  local swimmers_log_dir="${log_root}/swimmers"
  local swimmers_log_file="${swimmers_log_dir}/swimmers-server.log"
  local swimmers_pid_file="${swimmers_log_dir}/swimmers-server.pid"
  local metrics_url="http://127.0.0.1:${swimmers_port}/metrics"
  local sessions_url="http://127.0.0.1:${swimmers_port}/v1/sessions"

  ensure_paths() {
    mkdir -p "${swimmers_log_dir}" "${swimmers_install_dir}"
  }

  is_loopback_host() {
    case "${1:-}" in
      127.0.0.1|localhost|::1)
        return 0
        ;;
      *)
        return 1
        ;;
    esac
  }

  validate_remote_auth_shape() {
    if [[ "${swimmers_auth_mode}" == "token" && -z "${swimmers_auth_token}" ]]; then
      printf 'SKILLBOX_SWIMMERS_AUTH_MODE=token requires SKILLBOX_SWIMMERS_AUTH_TOKEN to be set\n' >&2
      return 1
    fi

    if ! is_loopback_host "${swimmers_publish_host}" && [[ "${swimmers_auth_mode}" != "token" ]]; then
      printf 'Refusing to expose swimmers on %s without token auth. Set SKILLBOX_SWIMMERS_AUTH_MODE=token and SKILLBOX_SWIMMERS_AUTH_TOKEN.\n' "${swimmers_publish_host}" >&2
      return 1
    fi
  }

  validate_https_download_url() {
    case "${1:-}" in
      https://*)
        return 0
        ;;
      *)
        printf 'Refusing to download swimmers over a non-HTTPS URL: %s\n' "${1:-}" >&2
        return 1
        ;;
    esac
  }

  normalize_sha256() {
    local value="${1:-}"
    value="$(printf '%s' "${value}" | tr '[:upper:]' '[:lower:]')"
    if [[ ! "${value}" =~ ^[0-9a-f]{64}$ ]]; then
      return 1
    fi
    printf '%s\n' "${value}"
  }

  sha256_file() {
    local path="${1:?missing file path}"
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum "${path}" | awk '{print $1}'
      return 0
    fi
    if command -v shasum >/dev/null 2>&1; then
      shasum -a 256 "${path}" | awk '{print $1}'
      return 0
    fi
    printf 'Neither sha256sum nor shasum is available to verify downloads\n' >&2
    return 1
  }

  binary_works() {
    local candidate="${1:?missing binary path}"
    "${candidate}" --help >/dev/null 2>&1
  }

  current_pid() {
    if [[ ! -f "${swimmers_pid_file}" ]]; then
      return 1
    fi

    local pid
    pid="$(tr -d '[:space:]' <"${swimmers_pid_file}")"
    if [[ -z "${pid}" ]]; then
      return 1
    fi

    if kill -0 "${pid}" >/dev/null 2>&1; then
      printf '%s\n' "${pid}"
      return 0
    fi

    return 1
  }

  metrics_ready() {
    local code
    code="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 1 --max-time 2 "${metrics_url}" 2>/dev/null || true)"
    [[ "${code}" == "200" ]]
  }

  sessions_probe_state() {
    local -a curl_args=(
      -sS
      -o /dev/null
      -w
      '%{http_code}'
      --connect-timeout
      1
      --max-time
      2
    )

    if [[ "${swimmers_auth_mode}" == "token" && -n "${swimmers_auth_token}" ]]; then
      curl_args+=(-H "Authorization: Bearer ${swimmers_auth_token}")
    fi

    curl "${curl_args[@]}" "${sessions_url}" 2>/dev/null || true
  }

  resolve_binary() {
    local candidate
    for candidate in \
      "${swimmers_bin}" \
      "${swimmers_repo}/target/release/swimmers"
    do
      if [[ -x "${candidate}" ]] && binary_works "${candidate}"; then
        printf '%s\n' "${candidate}"
        return 0
      fi
    done

    return 1
  }

  install_binary() {
    ensure_paths

    if [[ -x "${swimmers_bin}" ]] && binary_works "${swimmers_bin}"; then
      printf 'swimmers already installed at %s\n' "${swimmers_bin}"
      return 0
    fi

    if [[ -n "${swimmers_download_url}" ]]; then
      local expected_sha256
      if ! validate_https_download_url "${swimmers_download_url}"; then
        return 1
      fi
      expected_sha256="$(normalize_sha256 "${swimmers_download_sha256}")" || {
        printf 'SKILLBOX_SWIMMERS_DOWNLOAD_SHA256 must be set to a 64-character hex SHA-256 digest when download URL is configured\n' >&2
        return 1
      }
      local tmp_bin
      local actual_sha256
      tmp_bin="$(mktemp "${swimmers_install_dir}/swimmers.XXXXXX")"
      curl -fsSL "${swimmers_download_url}" -o "${tmp_bin}"
      actual_sha256="$(sha256_file "${tmp_bin}")" || {
        rm -f "${tmp_bin}"
        return 1
      }
      if [[ "${actual_sha256}" != "${expected_sha256}" ]]; then
        printf 'downloaded swimmers binary digest mismatch: expected %s, got %s\n' "${expected_sha256}" "${actual_sha256}" >&2
        rm -f "${tmp_bin}"
        return 1
      fi
      chmod +x "${tmp_bin}"
      if ! binary_works "${tmp_bin}"; then
        printf 'downloaded swimmers binary is not executable: %s\n' "${swimmers_download_url}" >&2
        rm -f "${tmp_bin}"
        return 1
      fi
      mv "${tmp_bin}" "${swimmers_bin}"
      printf 'installed swimmers from %s -> %s\n' "${swimmers_download_url}" "${swimmers_bin}"
      return 0
    fi

    if [[ ! -d "${swimmers_repo}" ]]; then
      printf 'swimmers repo is missing at %s and no download URL is configured\n' "${swimmers_repo}" >&2
      return 1
    fi

    if command -v cargo >/dev/null 2>&1 && [[ -f "${swimmers_repo}/Cargo.toml" ]]; then
      (
        cd "${swimmers_repo}"
        cargo build --release >/dev/null
      )
    fi

    if [[ -x "${swimmers_repo}/target/release/swimmers" ]] && binary_works "${swimmers_repo}/target/release/swimmers"; then
      cp "${swimmers_repo}/target/release/swimmers" "${swimmers_bin}"
      chmod +x "${swimmers_bin}"
      printf 'installed swimmers from %s -> %s\n' "${swimmers_repo}/target/release/swimmers" "${swimmers_bin}"
      return 0
    fi

    printf 'No usable swimmers binary found. Set SKILLBOX_SWIMMERS_DOWNLOAD_URL or provide a runnable %s.\n' "${swimmers_repo}/target/release/swimmers" >&2
    return 1
  }

  start_server() {
    ensure_paths
    validate_remote_auth_shape

    local pid
    if pid="$(current_pid)"; then
      printf 'swimmers already running (pid %s)\n' "${pid}"
      return 0
    fi

    local bin
    if ! bin="$(resolve_binary)"; then
      install_binary
      bin="$(resolve_binary)"
    fi

    : > "${swimmers_log_file}"
    local swimmers_run_dir="${workspace_root}"
    if [[ -d "${swimmers_repo}" ]]; then
      swimmers_run_dir="${swimmers_repo}"
    fi
    (
      cd "${swimmers_run_dir}"
      nohup env \
        PORT="${swimmers_port}" \
        AUTH_MODE="${swimmers_auth_mode}" \
        AUTH_TOKEN="${swimmers_auth_token}" \
        OBSERVER_TOKEN="${swimmers_observer_token}" \
        "${bin}" >>"${swimmers_log_file}" 2>&1 < /dev/null &
      echo "$!" > "${swimmers_pid_file}"
    )

    local attempts=0
    until metrics_ready; do
      attempts=$((attempts + 1))
      if (( attempts > 20 )); then
        printf 'timed out waiting for swimmers metrics at %s\n' "${metrics_url}" >&2
        tail -n 20 "${swimmers_log_file}" >&2 || true
        return 1
      fi
      sleep 1
    done

    printf 'swimmers ready on %s (published on %s:%s)\n' "${metrics_url}" "${swimmers_publish_host}" "${swimmers_port}"
  }

  stop_server() {
    local pid
    if ! pid="$(current_pid)"; then
      rm -f "${swimmers_pid_file}"
      printf 'swimmers is not running\n'
      return 0
    fi

    kill "${pid}" >/dev/null 2>&1 || true
    local attempts=0
    while kill -0 "${pid}" >/dev/null 2>&1; do
      attempts=$((attempts + 1))
      if (( attempts > 20 )); then
        printf 'swimmers did not stop cleanly; sending SIGKILL to %s\n' "${pid}" >&2
        kill -9 "${pid}" >/dev/null 2>&1 || true
        break
      fi
      sleep 0.5
    done

    rm -f "${swimmers_pid_file}"
    printf 'stopped swimmers (pid %s)\n' "${pid}"
  }

  show_status() {
    ensure_paths

    local pid="(not running)"
    if current_pid >/dev/null 2>&1; then
      pid="$(current_pid)"
    fi

    local metrics_state="down"
    if metrics_ready; then
      metrics_state="up"
    fi

    local sessions_state
    sessions_state="$(sessions_probe_state)"
    case "${sessions_state}" in
      200) sessions_state="ok" ;;
      401|403) sessions_state="auth-required" ;;
      *) sessions_state="down" ;;
    esac

    printf 'swimmers repo: %s\n' "${swimmers_repo}"
    printf 'swimmers bin: %s\n' "$(resolve_binary 2>/dev/null || printf '%s' "${swimmers_bin}")"
    printf 'publish host: %s\n' "${swimmers_publish_host}"
    printf 'port: %s\n' "${swimmers_port}"
    printf 'auth mode: %s\n' "${swimmers_auth_mode:-local-trust}"
    printf 'pid: %s\n' "${pid}"
    printf 'metrics: %s\n' "${metrics_state}"
    printf 'session access: %s\n' "${sessions_state}"
    printf 'log file: %s\n' "${swimmers_log_file}"
  }

  show_logs() {
    ensure_paths
    if [[ ! -f "${swimmers_log_file}" ]]; then
      printf 'no swimmers log file at %s\n' "${swimmers_log_file}" >&2
      return 1
    fi

    tail -n "${TAIL_LINES:-80}" -f "${swimmers_log_file}"
  }

  case "${command}" in
    install)
      install_binary
      ;;
    start)
      start_server
      ;;
    stop)
      stop_server
      ;;
    restart)
      stop_server
      start_server
      ;;
    status)
      show_status
      ;;
    logs)
      show_logs
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main() {
  if [[ "${1:-}" == "${INSIDE_FLAG}" ]]; then
    shift
    inside_main "$@"
    return
  fi

  local command="${1:-}"
  if [[ -z "${command}" ]]; then
    usage
    exit 1
  fi

  case "${command}" in
    install|start|restart|logs)
      ensure_workspace_running
      run_inside "${command}" "${@:2}"
      ;;
    stop|status)
      if ! workspace_running; then
        printf 'workspace container is not running\n'
        exit 0
      fi
      run_inside "${command}" "${@:2}"
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"
