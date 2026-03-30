#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./01-bootstrap-do.sh"
  exit 1
fi

APP_USER="${APP_USER:-skillbox}"

echo "[1/7] Updating OS packages..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get -y upgrade

echo "[2/7] Installing baseline packages..."
apt-get install -y \
  ca-certificates \
  curl \
  fail2ban \
  git \
  gnupg \
  jq \
  python3 \
  python3-yaml \
  tmux \
  ufw \
  unzip

echo "[3/7] Installing Node.js 22 LTS..."
if ! command -v node >/dev/null 2>&1 || [[ "$(node --version | cut -d. -f1 | tr -d v)" -lt 22 ]]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi
echo "  Node.js version: $(node --version)"

echo "[4/7] Installing Docker CE..."
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" | \
    tee /etc/apt/sources.list.d/docker.list >/dev/null
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
echo "  Docker version: $(docker --version)"

echo "[5/7] Creating app user (${APP_USER}) if missing..."
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "${APP_USER}"
fi
usermod -aG sudo,docker,adm "${APP_USER}"

echo "[6/7] Enabling host firewall with temporary SSH access..."
ufw --force default deny incoming
ufw --force default allow outgoing
ufw allow OpenSSH comment 'Temporary: narrowed in 02-install-tailscale.sh'
ufw allow 41641/udp comment 'Tailscale (optional direct path)'
ufw --force enable

echo "[7/7] Enabling fail2ban..."
systemctl enable --now fail2ban

echo
echo "Bootstrap complete."
echo "Next: run scripts/02-install-tailscale.sh to join the tailnet and narrow SSH."
