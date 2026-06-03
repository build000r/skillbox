#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT="${SKILLBOX_WORKSPACE_ROOT:-/workspace}"
REPOS_ROOT="${SKILLBOX_REPOS_ROOT:-${WORKSPACE_ROOT}/repos}"
SKILLS_ROOT="${SKILLBOX_SKILLS_ROOT:-${WORKSPACE_ROOT}/skills}"
LOG_ROOT="${SKILLBOX_LOG_ROOT:-${WORKSPACE_ROOT}/logs}"
HOME_ROOT="${SKILLBOX_HOME_ROOT:-/home/sandbox}"
MONOSERVER_ROOT="${SKILLBOX_MONOSERVER_ROOT:-/monoserver}"

mkdir -p \
  "${LOG_ROOT}/api" \
  "${LOG_ROOT}/runtime" \
  "${LOG_ROOT}/swimmers" \
  "${LOG_ROOT}/web" \
  "${REPOS_ROOT}" \
  "${SKILLS_ROOT}" \
  "${HOME_ROOT}/.claude" \
  "${HOME_ROOT}/.claude/skills" \
  "${HOME_ROOT}/.codex" \
  "${HOME_ROOT}/.codex/skills" \
  "${HOME_ROOT}/.config/claude-code" \
  "${HOME_ROOT}/.config/git" \
  "${HOME_ROOT}/.config/gh" \
  "${HOME_ROOT}/.grok" \
  "${HOME_ROOT}/.local/bin"

add_git_safe_directory() {
  local path="$1"
  local git_config="${HOME_ROOT}/.config/git/config"
  [[ -d "${path}" ]] || return 0
  if ! GIT_CONFIG_GLOBAL="${git_config}" git config --global --get-all safe.directory 2>/dev/null | grep -Fxq "${path}"; then
    GIT_CONFIG_GLOBAL="${git_config}" git config --global --add safe.directory "${path}" || true
  fi
}

if command -v git >/dev/null 2>&1; then
  add_git_safe_directory "${WORKSPACE_ROOT}"
  add_git_safe_directory "${REPOS_ROOT}"
  add_git_safe_directory "${MONOSERVER_ROOT}"
  if [[ -d "${MONOSERVER_ROOT}" ]]; then
    for repo in "${MONOSERVER_ROOT}"/*; do
      [[ -d "${repo}/.git" ]] && add_git_safe_directory "${repo}"
    done
  fi
fi

if [[ ! -f "${WORKSPACE_ROOT}/.env" ]]; then
  {
    echo "[skillbox-entrypoint] warning: ${WORKSPACE_ROOT}/.env is missing."
    echo "[skillbox-entrypoint] run: cp .env.example .env"
  } | tee -a "${LOG_ROOT}/runtime/entrypoint.log" >&2
else
  echo "[skillbox-entrypoint] env file detected at ${WORKSPACE_ROOT}/.env" >> "${LOG_ROOT}/runtime/entrypoint.log"
fi

exec "$@"
