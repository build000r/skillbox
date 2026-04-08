#!/usr/bin/env bash
set -euo pipefail

# 07-build-and-push-binary.sh — cross-compile a Rust binary for linux/amd64
# on the operator's Mac, scp it to a skillbox box, and symlink into PATH.
#
# Why this exists: s-2vcpu-4gb droplets OOM-thrash when compiling Rust on-box
# (knocks tailscaled offline, stalls SSH). Build on the Mac, ship the ELF,
# skip the pain. See scripts/01b-enable-swap.sh for the related hardening.
#
# Usage:
#   07-build-and-push-binary.sh <src-dir> <bin-name> <target> [cargo-package]
#
# Example (swimmers — no extra deps):
#   07-build-and-push-binary.sh ~/repos/opensource/swimmers swimmers skillbox-jeremy-3
#
# Example (fwc — needs apt deps and two /dp/ path-dep mounts):
#   SKILLBOX_BUILD_APT_DEPS="libdbus-1-dev pkg-config libssl-dev" \
#   SKILLBOX_BUILD_EXTRA_MOUNTS="$HOME/repos/opensource/asupersync:/dp/asupersync:ro $HOME/repos/opensource/toon_rust:/dp/toon_rust:ro" \
#   07-build-and-push-binary.sh \
#     ~/repos/opensource/skillbox/repos/flywheel_connectors \
#     fwc skillbox-jeremy-3
#
# Env vars:
#   SKILLBOX_BUILD_APT_DEPS     extra apt packages for the build container
#   SKILLBOX_BUILD_EXTRA_MOUNTS extra `docker -v` args, space-separated
#   SKILLBOX_BUILD_CACHE_ROOT   cargo cache root (default /tmp/skillbox-build)
#   SKILLBOX_BUILD_RUST_IMAGE   base rust image (default rust:1)
#   SKILLBOX_TARGET_BIN_DIR     where to land the binary on the box
#                               (default /home/skillbox/.local/bin)
#   SKILLBOX_SYMLINK_DIR        where to symlink it for PATH resolution
#                               (default /usr/local/bin — requires docker on box)
#   SKILLBOX_SSH_USER           ssh user on target (default skillbox)

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <src-dir> <bin-name> <target-host-or-ip> [cargo-package]" >&2
  exit 2
fi

SRC_DIR="$(cd "$1" && pwd)"
BIN_NAME="$2"
TARGET="$3"
PKG="${4:-$BIN_NAME}"

CACHE_ROOT="${SKILLBOX_BUILD_CACHE_ROOT:-/tmp/skillbox-build}"
RUST_IMAGE="${SKILLBOX_BUILD_RUST_IMAGE:-rust:1}"
TARGET_BIN_DIR="${SKILLBOX_TARGET_BIN_DIR:-/home/skillbox/.local/bin}"
SYMLINK_DIR="${SKILLBOX_SYMLINK_DIR:-/usr/local/bin}"
SSH_USER="${SKILLBOX_SSH_USER:-skillbox}"
EXTRA_APT="${SKILLBOX_BUILD_APT_DEPS:-}"
EXTRA_MOUNTS="${SKILLBOX_BUILD_EXTRA_MOUNTS:-}"

SSH_TARGET="${SSH_USER}@${TARGET}"

mkdir -p "${CACHE_ROOT}"/{cargo-registry,cargo-git,out} "${CACHE_ROOT}/${BIN_NAME}-target"

APT_CMD=""
if [[ -n "${EXTRA_APT}" ]]; then
  APT_CMD="apt-get update -qq && apt-get install -y -qq ${EXTRA_APT} && "
fi

MOUNT_ARGS=()
for m in ${EXTRA_MOUNTS}; do MOUNT_ARGS+=(-v "${m}"); done

echo "[1/4] Building ${BIN_NAME} (package=${PKG}) in ${RUST_IMAGE} for linux/amd64..."
docker run --rm --platform linux/amd64 \
  -v "${SRC_DIR}:/src:ro" \
  -v "${CACHE_ROOT}/${BIN_NAME}-target:/target" \
  -v "${CACHE_ROOT}/cargo-registry:/usr/local/cargo/registry" \
  -v "${CACHE_ROOT}/cargo-git:/usr/local/cargo/git" \
  -v "${CACHE_ROOT}/out:/out" \
  "${MOUNT_ARGS[@]}" \
  -e CARGO_TARGET_DIR=/target \
  -w /src \
  "${RUST_IMAGE}" bash -c \
  "${APT_CMD}cargo build --release --package ${PKG} && cp /target/release/${BIN_NAME} /out/${BIN_NAME} && file /out/${BIN_NAME}"

echo
echo "[2/4] Uploading to ${SSH_TARGET}:${TARGET_BIN_DIR}/${BIN_NAME}..."
ssh -o StrictHostKeyChecking=accept-new "${SSH_TARGET}" "mkdir -p ${TARGET_BIN_DIR}"
scp "${CACHE_ROOT}/out/${BIN_NAME}" "${SSH_TARGET}:${TARGET_BIN_DIR}/${BIN_NAME}"

echo
echo "[3/4] chmod +x and symlinking into ${SYMLINK_DIR} (via privileged docker escape — no sudo password required)..."
ssh "${SSH_TARGET}" "
  chmod +x '${TARGET_BIN_DIR}/${BIN_NAME}'
  docker run --rm --privileged -v /:/host alpine sh -c \
    'ln -sf ${TARGET_BIN_DIR}/${BIN_NAME} /host${SYMLINK_DIR}/${BIN_NAME}'
"

echo
echo "[4/4] Verifying on ${TARGET}..."
ssh "${SSH_TARGET}" "which ${BIN_NAME}; (${BIN_NAME} --version 2>&1 || ${BIN_NAME} --help 2>&1 | head -5)"

echo
echo "Done: ${BIN_NAME} installed on ${TARGET}."
