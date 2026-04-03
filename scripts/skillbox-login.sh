#!/usr/bin/env bash
# skillbox-login.sh — ForceCommand login hook for shared-jam collaboration.
# Resolves Tailscale identity from SSH peer IP, sets git author env vars,
# logs attributed commands, and creates/attaches a named tmux session.
#
# Deployed to /usr/local/bin/skillbox-login.sh by the provisioning process.
# Triggered via ForceCommand in sshd_config for the sandbox user.
set -euo pipefail

SHARED_HISTORY_LOG="/var/log/skillbox/shared-history.log"
WHOIS_TIMEOUT=3

# --- Dependency checks ---
for cmd in tailscale jq tmux; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "skillbox-login: required command '$cmd' not found. Install it first." >&2
    exit 1
  fi
done

# --- Identity resolution ---
resolve_identity() {
  local client_ip
  client_ip="$(echo "${SSH_CLIENT:-}" | awk '{print $1}')"

  if [[ -z "$client_ip" ]]; then
    echo "unknown"
    return
  fi

  local whois_json
  if ! whois_json="$(timeout "$WHOIS_TIMEOUT" tailscale whois --json "$client_ip" 2>/dev/null)"; then
    echo "unknown"
    return
  fi

  local login_name display_name
  login_name="$(echo "$whois_json" | jq -r '.UserProfile.LoginName // empty')"
  display_name="$(echo "$whois_json" | jq -r '.UserProfile.DisplayName // empty')"

  if [[ -z "$login_name" ]]; then
    echo "unknown"
    return
  fi

  # Export git author vars for the shell session
  export GIT_AUTHOR_NAME="${display_name:-$login_name}"
  export GIT_AUTHOR_EMAIL="$login_name"
  export GIT_COMMITTER_NAME="${display_name:-$login_name}"
  export GIT_COMMITTER_EMAIL="$login_name"

  echo "$login_name"
}

SKILLBOX_DEV="$(resolve_identity)"
export SKILLBOX_DEV

if [[ "$SKILLBOX_DEV" == "unknown" ]]; then
  echo "skillbox-login: WARNING — could not resolve Tailscale identity. Continuing as 'unknown'." >&2
fi

# --- Sanitize session name (strip @domain, replace non-alnum with dash) ---
sanitize_session_name() {
  local name="$1"
  name="${name%%@*}"
  name="$(echo "$name" | tr -cs '[:alnum:]' '-' | sed 's/^-//;s/-$//')"
  echo "${name:-unknown}"
}

SESSION_NAME="$(sanitize_session_name "$SKILLBOX_DEV")"

# --- Non-interactive session detection (scp, rsync, git-over-ssh) ---
# If SSH_ORIGINAL_COMMAND is set, this is a non-interactive command invocation.
# Pass it through with env vars set but skip tmux.
if [[ -n "${SSH_ORIGINAL_COMMAND:-}" ]]; then
  exec env \
    SKILLBOX_DEV="$SKILLBOX_DEV" \
    GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-unknown}" \
    GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-unknown}" \
    GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-unknown}" \
    GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-unknown}" \
    bash -c "$SSH_ORIGINAL_COMMAND"
fi

# --- PROMPT_COMMAND for shared history logging ---
PROMPT_CMD_EXPORT="$(cat <<'PROMPT_EOF'
__skillbox_log_cmd() {
  local last_cmd
  last_cmd="$(history 1 | sed 's/^[ ]*[0-9]*[ ]*//')"
  if [[ -n "$last_cmd" && -w "/var/log/skillbox/shared-history.log" ]]; then
    echo "$(date +%s) [${SKILLBOX_DEV}] ${last_cmd}" >> /var/log/skillbox/shared-history.log
  fi
}
PROMPT_COMMAND="__skillbox_log_cmd;${PROMPT_COMMAND:-}"
PROMPT_EOF
)"

# --- Launch or attach tmux session ---
exec tmux new-session -A -s "$SESSION_NAME" \
  "export SKILLBOX_DEV='${SKILLBOX_DEV}'; \
   export GIT_AUTHOR_NAME='${GIT_AUTHOR_NAME:-unknown}'; \
   export GIT_AUTHOR_EMAIL='${GIT_AUTHOR_EMAIL:-unknown}'; \
   export GIT_COMMITTER_NAME='${GIT_COMMITTER_NAME:-unknown}'; \
   export GIT_COMMITTER_EMAIL='${GIT_COMMITTER_EMAIL:-unknown}'; \
   ${PROMPT_CMD_EXPORT}; \
   exec bash"
