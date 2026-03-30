#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT="${SKILLBOX_WORKSPACE_ROOT:-/workspace}"
REPOS_ROOT="${SKILLBOX_REPOS_ROOT:-${WORKSPACE_ROOT}/repos}"
SKILLS_ROOT="${SKILLBOX_SKILLS_ROOT:-${WORKSPACE_ROOT}/skills}"
LOG_ROOT="${SKILLBOX_LOG_ROOT:-${WORKSPACE_ROOT}/logs}"
HOME_ROOT="${SKILLBOX_HOME_ROOT:-/home/sandbox}"

mkdir -p \
  "${LOG_ROOT}/api" \
  "${LOG_ROOT}/runtime" \
  "${LOG_ROOT}/web" \
  "${REPOS_ROOT}" \
  "${SKILLS_ROOT}" \
  "${HOME_ROOT}/.claude" \
  "${HOME_ROOT}/.codex"

if [[ ! -f "${WORKSPACE_ROOT}/.env" ]]; then
  {
    echo "[skillbox-entrypoint] warning: ${WORKSPACE_ROOT}/.env is missing."
    echo "[skillbox-entrypoint] run: cp .env.example .env"
  } | tee -a "${LOG_ROOT}/runtime/entrypoint.log" >&2
else
  echo "[skillbox-entrypoint] env file detected at ${WORKSPACE_ROOT}/.env" >> "${LOG_ROOT}/runtime/entrypoint.log"
fi

exec "$@"
