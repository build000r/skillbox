#!/usr/bin/env bash
# 03-shared-jam.sh — Invite, revoke, list, and status for shared-jam collaboration.
# Wraps Tailscale node sharing so the operator can manage collaborator access
# without touching the admin console.
set -euo pipefail

SHARED_HISTORY_LOG="/var/log/skillbox/shared-history.log"

# --- Root check ---
if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo scripts/03-shared-jam.sh <command> [args]"
  exit 1
fi

# --- Tailscale check ---
if ! command -v tailscale &>/dev/null; then
  echo "Tailscale not installed. Run scripts/02-install-tailscale.sh first."
  exit 1
fi

# --- Helpers ---
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

get_hostname() {
  tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // .Self.HostName' | sed 's/\.$//'
}

# --- Commands ---
cmd_invite() {
  local user="${1:-}"
  if [[ -z "$user" ]]; then
    echo "Usage: scripts/03-shared-jam.sh invite <tailscale-login>"
    exit 1
  fi

  # Check if already shared
  if tailscale share list 2>/dev/null | grep -qF "$user"; then
    echo "Already shared with ${user}."
    exit 0
  fi

  # Share the node. Tailscale node sharing uses `tailscale share` (Tailscale v1.56+).
  # Falls back to funnel-based sharing or documents manual steps if unavailable.
  if tailscale share create --with "$user" 2>/dev/null; then
    local hostname
    hostname="$(get_hostname)"
    echo "Shared with ${user}."
    echo "They should run: ssh sandbox@${hostname}"
  else
    echo "ERROR: 'tailscale share' failed." >&2
    echo "Your Tailscale plan may not support node sharing, or the CLI version may be too old." >&2
    echo "Manual alternative: share this node via https://login.tailscale.com/admin/machines" >&2
    exit 1
  fi
}

cmd_revoke() {
  local user="${1:-}"
  if [[ -z "$user" ]]; then
    echo "Usage: scripts/03-shared-jam.sh revoke <tailscale-login>"
    exit 1
  fi

  # Check if shared
  if ! tailscale share list 2>/dev/null | grep -qF "$user"; then
    echo "Not shared with ${user}."
    exit 0
  fi

  if tailscale share delete --with "$user" 2>/dev/null; then
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
  if [[ -z "$shares" ]]; then
    echo "No shared users."
  else
    echo "$shares"
  fi
}

cmd_status() {
  echo "=== Active tmux sessions ==="
  tmux ls 2>/dev/null || echo "(no tmux sessions)"
  echo
  echo "=== Recent shared history (last 20 lines) ==="
  if [[ -f "$SHARED_HISTORY_LOG" ]]; then
    tail -20 "$SHARED_HISTORY_LOG"
  else
    echo "(no shared history log yet)"
  fi
}

# --- Dispatch ---
case "${1:-}" in
  invite) cmd_invite "${2:-}" ;;
  revoke) cmd_revoke "${2:-}" ;;
  list)   cmd_list ;;
  status) cmd_status ;;
  *)      usage; exit 1 ;;
esac
