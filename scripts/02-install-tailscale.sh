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

ALLOW_USERS="${SSH_LOGIN_USER}"
if [[ -n "${EXTRA_SSH_LOGIN_USERS}" ]]; then
  ALLOW_USERS="${ALLOW_USERS} ${EXTRA_SSH_LOGIN_USERS}"
fi
ALLOW_USERS="$(echo "${ALLOW_USERS}" | xargs)"

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

if ! id -u "${SSH_LOGIN_USER}" >/dev/null 2>&1; then
  echo "SSH login user does not exist: ${SSH_LOGIN_USER}"
  echo "Set SSH_LOGIN_USER to an existing account before hardening SSH."
  exit 1
fi

echo "[5/8] Hardening sshd for Tailnet-only, non-root access..."
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

echo "[6/8] Installing shared-jam login hook..."
install -m 755 "$(dirname "$0")/skillbox-login.sh" /usr/local/bin/skillbox-login.sh

echo "[7/8] Creating shared history log directory..."
install -d -m 755 -o "${SSH_LOGIN_USER}" /var/log/skillbox

echo "[8/8] Restricting SSH firewall ingress to Tailnet..."
ufw allow from "${TAILNET_SSH_CIDR}" to any port 22 proto tcp comment 'Tailnet-only SSH'
ufw allow in on tailscale0 to any port 22 proto tcp comment 'Tailnet-only SSH (tailscale0)'
remove_openssh_ufw_rules
ufw --force reload

echo
echo "Tailscale setup complete."
echo "SSH is now Tailnet-only and root SSH login is disabled."
echo "Reminder: remove any public 22/tcp allow rule from the cloud firewall."
