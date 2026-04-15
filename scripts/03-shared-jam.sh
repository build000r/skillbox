#!/usr/bin/env bash
# 03-shared-jam.sh — Invite, revoke, list, and status for shared-jam collaboration.
# Wraps Tailscale node sharing so the operator can manage collaborator access
# without touching the admin console.
set -euo pipefail

SHARED_HISTORY_LOG="/var/log/skillbox/shared-history.log"
SHARED_JAM_SSHD_CONFIG="${SKILLBOX_SHARED_JAM_SSHD_CONFIG:-/etc/ssh/sshd_config.d/99-skillbox-tailnet.conf}"

usage() {
  cat <<EOF
Usage: scripts/03-shared-jam.sh <command> [args]

Commands:
  invite <tailscale-login>   Share this node with a Tailscale user
  revoke <tailscale-login>   Remove node share for a Tailscale user
  list                       List currently shared Tailscale users
  status                     Show active tmux sessions and recent history
EOF
}

ensure_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Run as root: sudo scripts/03-shared-jam.sh <command> [args]"
    exit 1
  fi
}

ensure_dependencies() {
  for cmd in tailscale jq; do
    if ! command -v "$cmd" &>/dev/null; then
      echo "Required command not found: $cmd" >&2
      exit 1
    fi
  done
}

get_hostname() {
  local status_json
  local hostname
  status_json="$(tailscale status --json 2>/dev/null || true)"
  hostname="$(printf '%s' "${status_json}" | jq -r '.Self.DNSName // .Self.HostName' 2>/dev/null || true)"
  hostname="${hostname%.}"
  printf '%s\n' "${hostname}"
}

get_shared_login_user() {
  local explicit="${SKILLBOX_SHARED_JAM_SSH_USER:-${SSH_LOGIN_USER:-}}"
  if [[ -n "${explicit}" ]]; then
    echo "${explicit}"
    return
  fi

  if [[ -f "${SHARED_JAM_SSHD_CONFIG}" ]]; then
    local configured
    configured="$(awk '/^[[:space:]]*Match[[:space:]]+User[[:space:]]+/ { print $3; exit }' "${SHARED_JAM_SSHD_CONFIG}" 2>/dev/null || true)"
    if [[ -n "${configured}" ]]; then
      echo "${configured}"
      return
    fi
  fi

  echo "skillbox"
}

suggest_box_id() {
  local raw="${1:-}"
  raw="${raw%%@*}"
  raw="$(echo "${raw}" | tr '[:upper:]' '[:lower:]' | tr -cs '[:alnum:]-' '-' | sed 's/^-//;s/-$//')"
  echo "${raw:-shared-box}"
}

build_register_command() {
  local invitee="${1:-}"
  local hostname="${2:-}"
  local ssh_user="${3:-}"
  local box_id
  box_id="$(suggest_box_id "${invitee}")"
  printf "python3 scripts/box.py register %q --host %q --ssh-user %q" "${box_id}" "${hostname}" "${ssh_user}"
}

print_access_handoff() {
  local invitee="${1:-}"
  local hostname="${2:-}"
  local ssh_user
  local register_cmd
  ssh_user="$(get_shared_login_user)"
  register_cmd="$(build_register_command "${invitee}" "${hostname}" "${ssh_user}")"
  echo "They should run: ssh ${ssh_user}@${hostname}"
  echo "If they want operator MCP on their machine after accepting the share, run:"
  echo "  ${register_cmd}"
}

cmd_invite() {
  local user="${1:-}"
  if [[ -z "${user}" ]]; then
    echo "Usage: scripts/03-shared-jam.sh invite <tailscale-login>"
    exit 1
  fi

  local hostname
  hostname="$(get_hostname)"

  if tailscale share list 2>/dev/null | grep -qF "${user}"; then
    echo "Already shared with ${user}."
    print_access_handoff "${user}" "${hostname}"
    exit 0
  fi

  if tailscale share create --with "${user}" 2>/dev/null; then
    echo "Shared with ${user}."
    print_access_handoff "${user}" "${hostname}"
  else
    echo "ERROR: 'tailscale share' failed." >&2
    echo "Your Tailscale plan may not support node sharing, or the CLI version may be too old." >&2
    echo "Manual alternative: share this node via https://login.tailscale.com/admin/machines" >&2
    exit 1
  fi
}

cmd_revoke() {
  local user="${1:-}"
  if [[ -z "${user}" ]]; then
    echo "Usage: scripts/03-shared-jam.sh revoke <tailscale-login>"
    exit 1
  fi

  if ! tailscale share list 2>/dev/null | grep -qF "${user}"; then
    echo "Not shared with ${user}."
    exit 0
  fi

  if tailscale share delete --with "${user}" 2>/dev/null; then
    echo "Revoked access for ${user}."
  else
    echo "ERROR: 'tailscale share delete' failed." >&2
    echo "Manual alternative: remove the share via https://login.tailscale.com/admin/machines" >&2
    exit 1
  fi
}

cmd_list() {
  local shares
  shares="$(tailscale share list 2>/dev/null)"
  if [[ -z "${shares}" ]]; then
    echo "No shared users."
  else
    echo "${shares}"
  fi
}

cmd_status() {
  echo "=== Active tmux sessions ==="
  tmux ls 2>/dev/null || echo "(no tmux sessions)"
  echo
  echo "=== Recent shared history (last 20 lines) ==="
  if [[ -f "${SHARED_HISTORY_LOG}" ]]; then
    tail -20 "${SHARED_HISTORY_LOG}"
  else
    echo "(no shared history log yet)"
  fi
}

main() {
  ensure_root
  ensure_dependencies

  case "${1:-}" in
    invite) cmd_invite "${2:-}" ;;
    revoke) cmd_revoke "${2:-}" ;;
    list)   cmd_list ;;
    status) cmd_status ;;
    *)      usage; exit 1 ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
