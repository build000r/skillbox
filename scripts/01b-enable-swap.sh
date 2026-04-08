#!/usr/bin/env bash
set -euo pipefail

# 01b-enable-swap.sh — idempotently provision a swapfile.
#
# Why this exists:
#   Small droplets (e.g. s-2vcpu-4gb) ship with zero swap. Running Docker +
#   cargo/rustc + tailscaled on 4 GB of RAM OOM-thrashes the box, which
#   knocks tailscaled offline and stalls new SSH handshakes — the exact
#   failure that took skillbox-jeremy-3 out on 2026-04-08.
#
# This script is safe to re-run: if /swapfile already exists and is active,
# it exits without changes.

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./01b-enable-swap.sh"
  exit 1
fi

SWAPFILE="${SWAPFILE:-/swapfile}"
SWAP_SIZE_GB="${SWAP_SIZE_GB:-4}"
SWAPPINESS="${SWAPPINESS:-10}"

echo "[1/5] Checking existing swap..."
if swapon --show=NAME --noheadings | grep -qx "${SWAPFILE}"; then
  echo "  ${SWAPFILE} already active; nothing to do."
  swapon --show
  exit 0
fi

echo "[2/5] Allocating ${SWAP_SIZE_GB}G at ${SWAPFILE}..."
if [[ ! -f "${SWAPFILE}" ]]; then
  if ! fallocate -l "${SWAP_SIZE_GB}G" "${SWAPFILE}" 2>/dev/null; then
    echo "  fallocate failed (unsupported fs?); falling back to dd..."
    dd if=/dev/zero of="${SWAPFILE}" bs=1M count=$((SWAP_SIZE_GB * 1024)) status=progress
  fi
fi
chmod 600 "${SWAPFILE}"

echo "[3/5] Formatting and enabling swap..."
if ! file "${SWAPFILE}" 2>/dev/null | grep -q "swap file"; then
  mkswap "${SWAPFILE}"
fi
swapon "${SWAPFILE}"

echo "[4/5] Persisting in /etc/fstab..."
if ! grep -qE "^[^#]*[[:space:]]${SWAPFILE//\//\\/}[[:space:]]" /etc/fstab; then
  echo "${SWAPFILE} none swap sw 0 0" >> /etc/fstab
fi

echo "[5/5] Tuning swappiness to ${SWAPPINESS}..."
sysctl -w "vm.swappiness=${SWAPPINESS}" >/dev/null
install -d -m 0755 /etc/sysctl.d
cat >/etc/sysctl.d/99-skillbox-swap.conf <<EOF
# Managed by skillbox scripts/01b-enable-swap.sh
vm.swappiness=${SWAPPINESS}
EOF

echo
echo "Swap enabled:"
swapon --show
free -m
