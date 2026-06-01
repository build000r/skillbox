#!/usr/bin/env bash
# Called by Claude SessionEnd hooks, codex-tmux completion, and cron fallback.
# This must never block or fail session teardown.

set -uo pipefail

SOURCE="both"
SINCE="marker"
LIMIT="1"
DRY_RUN=0
SKILLS=()

log() {
  printf 'score-session: %s\n' "$*" >&2
}

split_skill_list() {
  local raw="$1"
  raw="${raw//,/ }"
  for skill in $raw; do
    if [[ -n "$skill" ]]; then
      SKILLS+=("$skill")
    fi
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      SOURCE="${2:-both}"
      shift 2
      ;;
    --source=*)
      SOURCE="${1#--source=}"
      shift
      ;;
    --since)
      SINCE="${2:-marker}"
      shift 2
      ;;
    --since=*)
      SINCE="${1#--since=}"
      shift
      ;;
    --limit)
      LIMIT="${2:-1}"
      shift 2
      ;;
    --limit=*)
      LIMIT="${1#--limit=}"
      shift
      ;;
    --skill)
      SKILLS+=("${2:-}")
      shift 2
      ;;
    --skill=*)
      SKILLS+=("${1#--skill=}")
      shift
      ;;
    --skills)
      split_skill_list "${2:-}"
      shift 2
      ;;
    --skills=*)
      split_skill_list "${1#--skills=}"
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --transcript|--session-id)
      shift 2
      ;;
    --transcript=*|--session-id=*)
      shift
      ;;
    *)
      # Hook integrations may grow extra metadata before this wrapper does.
      shift
      ;;
  esac
done

case "$SOURCE" in
  claude|codex|both|all) ;;
  *) SOURCE="both" ;;
esac

if [[ ! "$LIMIT" =~ ^[0-9]+$ ]] || [[ "$LIMIT" -lt 1 ]]; then
  LIMIT="1"
fi

find_skill_issue_dir() {
  local candidate
  for candidate in \
    "$HOME/.claude/skills/skill-issue" \
    "$HOME/.codex/skills/skill-issue" \
    "${SKILLBOX_SKILL_ISSUE_DIR:-}"
  do
    if [[ -n "$candidate" && -f "$candidate/scripts/review_skill_usage.py" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

discover_skills() {
  local root skill_dir skill_name
  if [[ -n "${SKILLBOX_FORGE_SKILLS:-}" ]]; then
    split_skill_list "$SKILLBOX_FORGE_SKILLS"
    return 0
  fi
  for root in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
    [[ -d "$root" ]] || continue
    while IFS= read -r skill_dir; do
      skill_name="$(basename "$skill_dir")"
      [[ "$skill_name" == "skill-issue" ]] && continue
      SKILLS+=("$skill_name")
    done < <(find "$root" -mindepth 1 -maxdepth 1 -type d -exec test -f '{}/SKILL.md' ';' -print 2>/dev/null)
  done
}

unique_skills() {
  local skill
  local seen=" "
  local unique=()
  for skill in "${SKILLS[@]}"; do
    [[ -n "$skill" ]] || continue
    if [[ "$seen" != *" $skill "* ]]; then
      seen="$seen$skill "
      unique+=("$skill")
    fi
  done
  SKILLS=("${unique[@]}")
}

skill_has_invocations() {
  python3 - "$1" <<'PY'
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if int(payload.get("invocations_found") or 0) > 0 else 1)
PY
}

main() {
  local skill_issue_dir review_script save_script
  skill_issue_dir="$(find_skill_issue_dir)" || {
    log "skill-issue not installed, skipping"
    return 0
  }
  review_script="$skill_issue_dir/scripts/review_skill_usage.py"
  save_script="$skill_issue_dir/scripts/save_skill_review.py"
  if [[ ! -f "$save_script" ]]; then
    log "save_skill_review.py missing, skipping"
    return 0
  fi

  discover_skills
  unique_skills
  if [[ "${#SKILLS[@]}" -eq 0 ]]; then
    log "no installed skills discovered, skipping"
    return 0
  fi

  local skill tmp status saved=0 reviewed=0
  for skill in "${SKILLS[@]}"; do
    tmp="$(mktemp "${TMPDIR:-/tmp}/skill-forge-review.XXXXXX.json")" || continue
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "dry-run would review $skill from $SOURCE since $SINCE"
      rm -f "$tmp"
      continue
    fi
    python3 "$review_script" \
      --skill "$skill" \
      --source "$SOURCE" \
      --limit "$LIMIT" \
      --since "$SINCE" \
      >"$tmp" 2>/dev/null
    status=$?
    if [[ "$status" -eq 0 ]]; then
      reviewed=$((reviewed + 1))
      if skill_has_invocations "$tmp" || [[ "${SKILLBOX_FORGE_SAVE_EMPTY:-0}" == "1" ]]; then
        python3 "$save_script" --input "$tmp" >/dev/null 2>&1 || true
        saved=$((saved + 1))
      fi
    fi
    rm -f "$tmp"
  done
  log "reviewed=$reviewed saved=$saved"
  return 0
}

main "$@" || true
exit 0
