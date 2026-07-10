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
APT_LOCK_TIMEOUT_SECONDS="${APT_LOCK_TIMEOUT_SECONDS:-300}"

wait_for_apt_readiness() {
  local deadline=$((SECONDS + APT_LOCK_TIMEOUT_SECONDS))

  if command -v cloud-init >/dev/null 2>&1; then
    echo "Waiting for cloud-init to finish..."
    cloud-init status --wait || true
  fi

  while fuser /var/lib/dpkg/lock >/dev/null 2>&1 \
    || fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
    || fuser /var/lib/apt/lists/lock >/dev/null 2>&1 \
    || pgrep -x apt >/dev/null 2>&1 \
    || pgrep -x apt-get >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      echo "Timed out waiting for apt/dpkg locks to clear." >&2
      return 1
    fi
    sleep 5
  done
}

if [[ -n "${VOLUME_NAME}" ]]; then
  VOLUME_DEVICE="/dev/disk/by-id/scsi-0DO_Volume_${VOLUME_NAME}"
fi

echo "[1/9] Updating OS packages..."
wait_for_apt_readiness
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get -y upgrade

echo "[2/9] Installing baseline packages..."
apt-get install -y \
  bat \
  build-essential \
  ca-certificates \
  curl \
  direnv \
  fail2ban \
  fd-find \
  fzf \
  gh \
  git \
  golang-go \
  gnupg \
  jq \
  less \
  libssl-dev \
  make \
  openssh-client \
  pipx \
  pkg-config \
  python3 \
  python3-pip \
  python3-venv \
  python3-yaml \
  ripgrep \
  rustc \
  cargo \
  tmux \
  ufw \
  unzip \
  zsh \
  xfsprogs

echo "[3/9] Installing Node.js 22 LTS..."
if ! command -v node >/dev/null 2>&1 || [[ "$(node --version | cut -d. -f1 | tr -d v)" -lt 22 ]]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi
echo "  Node.js version: $(node --version)"

echo "[4/9] Installing Docker CE..."
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

echo "[5/9] Creating app user (${APP_USER}) if missing..."
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "${APP_USER}"
fi
usermod -aG sudo,docker,adm "${APP_USER}"
install -d -m 700 -o "${APP_USER}" -g "${APP_USER}" "/home/${APP_USER}/.ssh"
install -d -m 755 -o "${APP_USER}" -g "${APP_USER}" "/home/${APP_USER}/.local" "/home/${APP_USER}/.local/bin"
install -d -m 700 -o "${APP_USER}" -g "${APP_USER}" \
  "/home/${APP_USER}/.config" \
  "/home/${APP_USER}/.config/claude-code" \
  "/home/${APP_USER}/.config/git" \
  "/home/${APP_USER}/.config/gh" \
  "/home/${APP_USER}/.grok"
cat >"/home/${APP_USER}/.profile.d-skillbox-paths" <<'EOF'
# Skillbox operator shell baseline.
for dir in "$HOME/.grok/bin" "$HOME/.npm-global/bin" "$HOME/.cargo/bin" "$HOME/.local/bin" "$HOME/bin"; do
  case ":$PATH:" in
    *":$dir:"*) ;;
    *) [ -d "$dir" ] && PATH="$dir:$PATH" ;;
  esac
done
export PATH
EOF
chown "${APP_USER}:${APP_USER}" "/home/${APP_USER}/.profile.d-skillbox-paths"
chmod 644 "/home/${APP_USER}/.profile.d-skillbox-paths"
bashrc_file="/home/${APP_USER}/.bashrc"
if [[ -f "${bashrc_file}" ]] && ! grep -qF '. "$HOME/.profile.d-skillbox-paths"' "${bashrc_file}"; then
  bashrc_tmp="$(mktemp)"
  {
    echo '# Skillbox operator PATH baseline'
    echo '[ -f "$HOME/.profile.d-skillbox-paths" ] && . "$HOME/.profile.d-skillbox-paths"'
    echo
    cat "${bashrc_file}"
  } >"${bashrc_tmp}"
  install -m 644 -o "${APP_USER}" -g "${APP_USER}" "${bashrc_tmp}" "${bashrc_file}"
  rm -f "${bashrc_tmp}"
fi
profile_file="/home/${APP_USER}/.profile"
if [[ -f "${profile_file}" ]] && ! grep -qF '. "$HOME/.profile.d-skillbox-paths"' "${profile_file}"; then
  {
    echo
    echo '# Skillbox operator PATH baseline'
    echo '[ -f "$HOME/.profile.d-skillbox-paths" ] && . "$HOME/.profile.d-skillbox-paths"'
  } >>"${profile_file}"
fi
if command -v fdfind >/dev/null 2>&1 && [[ ! -e "/home/${APP_USER}/.local/bin/fd" ]]; then
  ln -sf /usr/bin/fdfind "/home/${APP_USER}/.local/bin/fd"
fi
if command -v batcat >/dev/null 2>&1 && [[ ! -e "/home/${APP_USER}/.local/bin/bat" ]]; then
  ln -sf /usr/bin/batcat "/home/${APP_USER}/.local/bin/bat"
fi
chown -h "${APP_USER}:${APP_USER}" "/home/${APP_USER}/.local/bin/fd" "/home/${APP_USER}/.local/bin/bat" 2>/dev/null || true
if [[ -f /root/.ssh/authorized_keys ]]; then
  install -m 600 -o "${APP_USER}" -g "${APP_USER}" /root/.ssh/authorized_keys "/home/${APP_USER}/.ssh/authorized_keys"
fi

echo "[6/9] Preparing durable state root..."
if [[ -n "${STATE_ROOT}" && -n "${STORAGE_FILESYSTEM}" && -n "${VOLUME_DEVICE}" ]]; then
  for _attempt in $(seq 1 30); do
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

  SANDBOX_GID="${SANDBOX_GID:-1001}"
  chown "${APP_USER}:${APP_USER}" "${STATE_ROOT}"
  install -d -o "${APP_USER}" -g "${SANDBOX_GID}" -m 2775 \
    "${STATE_ROOT}/home/.claude" \
    "${STATE_ROOT}/home/.codex" \
    "${STATE_ROOT}/home/.config" \
    "${STATE_ROOT}/home/.config/claude-code" \
    "${STATE_ROOT}/home/.config/git" \
    "${STATE_ROOT}/home/.config/gh" \
    "${STATE_ROOT}/home/.grok" \
    "${STATE_ROOT}/home/.local" \
    "${STATE_ROOT}/backups" \
    "${STATE_ROOT}/clients" \
    "${STATE_ROOT}/logs" \
    "${STATE_ROOT}/repos" \
    "${STATE_ROOT}/monoserver"
  for shared_dir in \
    "${STATE_ROOT}/home/.claude" \
    "${STATE_ROOT}/home/.codex" \
    "${STATE_ROOT}/home/.config" \
    "${STATE_ROOT}/home/.grok" \
    "${STATE_ROOT}/home/.local" \
    "${STATE_ROOT}/backups" \
    "${STATE_ROOT}/clients" \
    "${STATE_ROOT}/logs" \
    "${STATE_ROOT}/repos" \
    "${STATE_ROOT}/monoserver"; do
    chown -R "${APP_USER}:${SANDBOX_GID}" "${shared_dir}"
    chmod -R u+rwX,g+rwX "${shared_dir}"
    find "${shared_dir}" -type d -exec chmod g+s {} +
  done
  if [[ "$(dirname "${STATE_ROOT}")" == "/srv" ]]; then
    state_name="$(basename "${STATE_ROOT}")"
    for alias_name in repos home logs backups; do
      alias_path="/srv/${alias_name}"
      alias_target="${state_name}/${alias_name}"
      if [[ -L "${alias_path}" ]]; then
        ln -sfn "${alias_target}" "${alias_path}"
      elif [[ ! -e "${alias_path}" ]]; then
        ln -s "${alias_target}" "${alias_path}"
      fi
    done
  fi
else
  echo "  No state-volume metadata provided; skipping durable-state mount setup."
fi

echo "[7/9] Enabling host firewall with temporary SSH access..."
ufw --force default deny incoming
ufw --force default allow outgoing
ufw allow OpenSSH comment 'Temporary: narrowed in 02-install-tailscale.sh'
ufw allow 41641/udp comment 'Tailscale (optional direct path)'
ufw --force enable

echo "[8/9] Enabling fail2ban..."
systemctl enable --now fail2ban

echo "[9/9] Enabling swapfile (delegates to 01b-enable-swap.sh)..."
swap_helper="$(dirname "$0")/01b-enable-swap.sh"
if [[ -f "${swap_helper}" ]]; then
  bash "${swap_helper}"
else
  swapfile="${SWAPFILE:-/swapfile}"
  swapsize="${SWAPSIZE:-2G}"
  if swapon --show=NAME --noheadings | grep -qx "${swapfile}"; then
    echo "  Swap already active at ${swapfile}."
  else
    if [[ ! -f "${swapfile}" ]]; then
      if command -v fallocate >/dev/null 2>&1; then
        fallocate -l "${swapsize}" "${swapfile}"
      else
        dd if=/dev/zero of="${swapfile}" bs=1M count=2048 status=none
      fi
      chmod 600 "${swapfile}"
      mkswap "${swapfile}" >/dev/null
    fi
    swapon "${swapfile}"
  fi
  grep -qE "^[^#[:space:]]+[[:space:]]+none[[:space:]]+swap[[:space:]]" /etc/fstab \
    || echo "${swapfile} none swap sw 0 0" >> /etc/fstab
  echo "  Swap enabled at ${swapfile}."
fi

echo
echo "Bootstrap complete."
echo "Next: run scripts/02-install-tailscale.sh to join the tailnet and narrow SSH."
