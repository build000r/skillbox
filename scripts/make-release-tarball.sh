#!/usr/bin/env bash
# make-release-tarball.sh — build a reproducible source tarball for a release
# tag plus a SHA256SUMS manifest, ready to attach as GitHub release assets.
#
# The installer's git-less curl fallback fetches these published assets and
# verifies the checksum by default (no user-supplied --sha256 needed). See
# install.sh:download_release_assets.
#
# Usage:
#   scripts/make-release-tarball.sh <tag> [output-dir]
#
# Produces, under <output-dir> (default: ./dist):
#   skillbox-<tag>.tar.gz   source tree at <tag>, top-level dir skillbox-<tag>/
#   SHA256SUMS              sha256 manifest covering skillbox-<tag>.tar.gz
#
# The tarball is built from `git archive` so it contains exactly the tracked
# tree at <tag> (no .git, no untracked cruft) and is byte-stable for a given
# commit. Sign the tarball separately (e.g. cosign sign-blob --bundle
# skillbox-<tag>.tar.gz.sigstore.json skillbox-<tag>.tar.gz) in the release
# workflow so the installer can pin-verify the signature.
set -euo pipefail

TAG="${1:-}"
OUT_DIR="${2:-dist}"

if [[ -z "${TAG}" ]]; then
  echo "usage: $0 <tag> [output-dir]" >&2
  exit 1
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "${REPO_ROOT}"

if ! git rev-parse -q --verify "${TAG}^{commit}" >/dev/null 2>&1; then
  echo "error: ${TAG} is not a valid git ref in this repository" >&2
  exit 1
fi

mkdir -p "${OUT_DIR}"
OUT_DIR="$(cd "${OUT_DIR}" && pwd)"

TARBALL="${OUT_DIR}/skillbox-${TAG}.tar.gz"
PREFIX="skillbox-${TAG}/"

# git archive is deterministic for a fixed commit; pipe through gzip -n so the
# gzip header carries no mtime/name, keeping the artifact byte-stable.
git archive --format=tar --prefix="${PREFIX}" "${TAG}" | gzip -n >"${TARBALL}"

# Emit a SHA256SUMS manifest with a repo-relative filename (no directory
# component) so `sha256sum -c SHA256SUMS` works from the output dir and the
# installer can match on the bare asset name.
(
  cd "${OUT_DIR}"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "skillbox-${TAG}.tar.gz" >SHA256SUMS
  else
    shasum -a 256 "skillbox-${TAG}.tar.gz" >SHA256SUMS
  fi
)

echo "Wrote ${TARBALL}"
echo "Wrote ${OUT_DIR}/SHA256SUMS"
cat "${OUT_DIR}/SHA256SUMS"
