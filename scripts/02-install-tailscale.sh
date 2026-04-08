#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./02-install-tailscale.sh"
  exit 1
fi

TAILSCALE_AUTHKEY="${TAILSCALE_AUTHKEY:-}"
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-skillbox-dev}"
TAILNET_SSH_CIDR="${TAILNET_SSH_CIDR:-100.64.0.0/10}"
SSH_LOGIN_USER="${SSH_LOGIN_USER:-skillbox}"
EXTRA_SSH_LOGIN_USERS="${EXTRA_SSH_LOGIN_USERS:-}"
TAILNET_ONLY_SSH="${TAILNET_ONLY_SSH:-false}"

ALLOW_USERS="${SSH_LOGIN_USER}"
if [[ -n "${EXTRA_SSH_LOGIN_USERS}" ]]; then
  ALLOW_USERS="${ALLOW_USERS} ${EXTRA_SSH_LOGIN_USERS}"
fi
ALLOW_USERS="$(echo "${ALLOW_USERS}" | xargs)"

install_login_hook() {
  cat >/usr/local/bin/skillbox-login.sh <<'EOF'
#!/usr/bin/env bash
# skillbox-login.sh — ForceCommand login hook for shared-jam collaboration.
set -euo pipefail

SHARED_HISTORY_LOG="/var/log/skillbox/shared-history.log"
WHOIS_TIMEOUT=3

for cmd in tailscale jq tmux; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "skillbox-login: required command '$cmd' not found. Install it first." >&2
    exit 1
  fi
done

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

sanitize_session_name() {
  local name="$1"
  name="${name%%@*}"
  name="$(echo "$name" | tr -cs '[:alnum:]' '-' | sed 's/^-//;s/-$//')"
  echo "${name:-unknown}"
}

SESSION_NAME="$(sanitize_session_name "$SKILLBOX_DEV")"

if [[ -n "${SSH_ORIGINAL_COMMAND:-}" ]]; then
  exec env \
    SKILLBOX_DEV="$SKILLBOX_DEV" \
    GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-unknown}" \
    GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-unknown}" \
    GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-unknown}" \
    GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-unknown}" \
    bash -c "$SSH_ORIGINAL_COMMAND"
fi

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

exec tmux new-session -A -s "$SESSION_NAME" \
  "export SKILLBOX_DEV='${SKILLBOX_DEV}'; \
   export GIT_AUTHOR_NAME='${GIT_AUTHOR_NAME:-unknown}'; \
   export GIT_AUTHOR_EMAIL='${GIT_AUTHOR_EMAIL:-unknown}'; \
   export GIT_COMMITTER_NAME='${GIT_COMMITTER_NAME:-unknown}'; \
   export GIT_COMMITTER_EMAIL='${GIT_COMMITTER_EMAIL:-unknown}'; \
   ${PROMPT_CMD_EXPORT}; \
   exec bash"
EOF
  chmod 755 /usr/local/bin/skillbox-login.sh
}

remove_openssh_ufw_rules() {
  local rule_num
  while true; do
    rule_num="$(ufw status numbered 2>/dev/null | awk '/OpenSSH/ {gsub(/\[/,"",$1); gsub(/\]/,"",$1); print $1; exit}')"
    if [[ -z "${rule_num}" ]]; then
      break
    fi
    ufw --force delete "${rule_num}" >/dev/null 2>&1 || break
  done
}

echo "[1/8] Installing Tailscale..."
curl -fsSL https://tailscale.com/install.sh | sh

echo "[2/8] Starting tailscaled..."
systemctl enable --now tailscaled

echo "[3/8] Joining tailnet..."
if [[ -n "${TAILSCALE_AUTHKEY}" ]]; then
  tailscale up \
    --authkey="${TAILSCALE_AUTHKEY}" \
    --hostname="${TAILSCALE_HOSTNAME}" \
    --ssh \
    --accept-routes=false \
    --accept-dns=false
else
  tailscale up \
    --hostname="${TAILSCALE_HOSTNAME}" \
    --ssh \
    --accept-routes=false \
    --accept-dns=false
fi

echo "[4/8] Tailnet status:"
tailscale status
TAILSCALE_IPV4="$(tailscale ip -4 | head -n1)"
if [[ -z "${TAILSCALE_IPV4}" ]]; then
  echo "Tailscale did not report an IPv4 address after enrollment." >&2
  exit 1
fi
echo "TAILSCALE_IPV4=${TAILSCALE_IPV4}"

if ! id -u "${SSH_LOGIN_USER}" >/dev/null 2>&1; then
  echo "SSH login user does not exist: ${SSH_LOGIN_USER}"
  echo "Set SSH_LOGIN_USER to an existing account before hardening SSH."
  exit 1
fi

echo "[5/8] Installing shared-jam login hook..."
install_login_hook

echo "[6/8] Creating shared history log directory..."
install -d -m 755 -o "${SSH_LOGIN_USER}" /var/log/skillbox

echo "[7/8] Hardening sshd for Tailnet-only, non-root access..."
install -d -m 755 /etc/ssh/sshd_config.d
cat >/etc/ssh/sshd_config.d/99-skillbox-tailnet.conf <<EOF
# Managed by skillbox scripts/02-install-tailscale.sh
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
AllowUsers ${ALLOW_USERS}

# Shared-jam: route sandbox user SSH sessions through the login hook
Match User ${SSH_LOGIN_USER}
  ForceCommand /usr/local/bin/skillbox-login.sh
EOF

sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true

echo "[8/8] Restricting SSH firewall ingress to Tailnet..."
if [[ "${TAILNET_ONLY_SSH}" == "true" ]]; then
  ufw allow from "${TAILNET_SSH_CIDR}" to any port 22 proto tcp comment 'Tailnet-only SSH'
  ufw allow in on tailscale0 to any port 22 proto tcp comment 'Tailnet-only SSH (tailscale0)'
  remove_openssh_ufw_rules
  ufw --force reload
else
  echo "  Keeping public OpenSSH access enabled for bootstrap/deploy fallback."
fi

echo
echo "Tailscale setup complete."
if [[ "${TAILNET_ONLY_SSH}" == "true" ]]; then
  echo "SSH is now Tailnet-only and root SSH login is disabled."
  echo "Reminder: remove any public 22/tcp allow rule from the cloud firewall."
else
  echo "Root SSH login is disabled; ${SSH_LOGIN_USER} remains reachable via public SSH until tailnet-only hardening is applied."
fi
