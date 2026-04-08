#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./01-bootstrap-do.sh"
  exit 1
fi

APP_USER="${APP_USER:-skillbox}"
STATE_ROOT="${SKILLBOX_STATE_ROOT:-}"
STORAGE_FILESYSTEM="${SKILLBOX_STORAGE_FILESYSTEM:-}"
VOLUME_NAME="${SKILLBOX_VOLUME_NAME:-}"
VOLUME_DEVICE=""

if [[ -n "${VOLUME_NAME}" ]]; then
  VOLUME_DEVICE="/dev/disk/by-id/scsi-0DO_Volume_${VOLUME_NAME}"
fi

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
  unzip \
  xfsprogs

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

echo "[5/8] Creating app user (${APP_USER}) if missing..."
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "${APP_USER}"
fi
usermod -aG sudo,docker,adm "${APP_USER}"

echo "[6/8] Preparing durable state root..."
if [[ -n "${STATE_ROOT}" && -n "${STORAGE_FILESYSTEM}" && -n "${VOLUME_DEVICE}" ]]; then
  for attempt in $(seq 1 30); do
    if [[ -b "${VOLUME_DEVICE}" ]]; then
      break
    fi
    sleep 2
  done

  if [[ ! -b "${VOLUME_DEVICE}" ]]; then
    echo "State volume device did not appear: ${VOLUME_DEVICE}" >&2
    exit 1
  fi

  install -d -m 0755 "${STATE_ROOT}"
  existing_fs="$(blkid -o value -s TYPE "${VOLUME_DEVICE}" 2>/dev/null || true)"
  if [[ -z "${existing_fs}" ]]; then
    case "${STORAGE_FILESYSTEM}" in
      ext4)
        mkfs.ext4 -F "${VOLUME_DEVICE}"
        ;;
      xfs)
        mkfs.xfs -f "${VOLUME_DEVICE}"
        ;;
      *)
        echo "Unsupported filesystem type: ${STORAGE_FILESYSTEM}" >&2
        exit 1
        ;;
    esac
  elif [[ "${existing_fs}" != "${STORAGE_FILESYSTEM}" ]]; then
    echo "State volume filesystem mismatch: found ${existing_fs}, expected ${STORAGE_FILESYSTEM}" >&2
    exit 1
  fi

  sed -i "\|[[:space:]]${STATE_ROOT}[[:space:]]|d" /etc/fstab
  echo "${VOLUME_DEVICE} ${STATE_ROOT} ${STORAGE_FILESYSTEM} defaults,nofail,discard,noatime 0 2" >> /etc/fstab
  mountpoint -q "${STATE_ROOT}" || mount -o defaults,nofail,discard,noatime "${VOLUME_DEVICE}" "${STATE_ROOT}"
  findmnt --verify --verbose
  findmnt "${STATE_ROOT}"

  chown "${APP_USER}:${APP_USER}" "${STATE_ROOT}"
  install -d -o "${APP_USER}" -g "${APP_USER}" -m 0755 \
    "${STATE_ROOT}/home/.claude" \
    "${STATE_ROOT}/home/.codex" \
    "${STATE_ROOT}/home/.local" \
    "${STATE_ROOT}/clients" \
    "${STATE_ROOT}/logs" \
    "${STATE_ROOT}/monoserver"
else
  echo "  No state-volume metadata provided; skipping durable-state mount setup."
fi

echo "[7/8] Enabling host firewall with temporary SSH access..."
ufw --force default deny incoming
ufw --force default allow outgoing
ufw allow OpenSSH comment 'Temporary: narrowed in 02-install-tailscale.sh'
ufw allow 41641/udp comment 'Tailscale (optional direct path)'
ufw --force enable

echo "[8/8] Enabling fail2ban..."
systemctl enable --now fail2ban

echo
echo "Bootstrap complete."
echo "Next: run scripts/02-install-tailscale.sh to join the tailnet and narrow SSH."
