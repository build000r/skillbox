#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/03-skill-sync.sh [options]

Options:
  --manifest <path>     Plain-text manifest listing skill names only.
  --sources <path>      YAML source config listing repo pins and local roots.
  --output-dir <path>   Directory to write packaged .skill files into.
  --packager <path>     Override package_skill.py path.
  --no-clear            Keep existing .skill files in the output directory.
  --dry-run             Resolve and print what would be packaged.
  -h, --help            Show this help.

Manifest syntax:
  ask-cascade
  describe
  reproduce

Source config syntax:
  version: 1
  sources:
    - kind: github
      repo: https://github.com/build000r/skills
      sha: <commit-sha>
    - kind: local
      path: ./skills
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

MANIFEST_FILE="${ROOT_DIR}/workspace/default-skills.manifest"
SOURCES_FILE="${ROOT_DIR}/workspace/default-skills.sources.yaml"
OUTPUT_DIR="${ROOT_DIR}/default-skills"
CACHE_DIR="${ROOT_DIR}/.cache/skill-sync"
PACKAGER="${ROOT_DIR}/scripts/package_skill.py"
CLEAR_OUTPUT=1
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest)
      MANIFEST_FILE="$2"
      shift 2
      ;;
    --sources)
      SOURCES_FILE="$2"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --packager)
      PACKAGER="$2"
      shift 2
      ;;
    --no-clear)
      CLEAR_OUTPUT=0
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

expand_path() {
  local raw="$1"
  if [[ "$raw" == "~/"* ]]; then
    printf '%s\n' "${HOME}/${raw#~/}"
  else
    printf '%s\n' "$raw"
  fi
}

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s\n' "$value"
}

build_github_archive_url() {
  local repo_url="$1"
  local sha="$2"

  repo_url="${repo_url%.git}"
  repo_url="${repo_url%/}"
  printf '%s/archive/%s.tar.gz\n' "$repo_url" "$sha"
}

prepare_github_source() {
  local repo_url="$1"
  local sha="$2"
  local repo_slug
  local cache_root
  local archive_file
  local extract_dir
  local extracted_root

  repo_slug="$(printf '%s' "${repo_url%.git}" | sed -E 's#https?://##; s#[^A-Za-z0-9._-]+#-#g')"
  cache_root="${CACHE_DIR}/${repo_slug}-${sha}"
  archive_file="${cache_root}/repo.tar.gz"
  extract_dir="${cache_root}/extracted"

  if [[ ! -d "$extract_dir" ]]; then
    rm -rf "$cache_root"
    mkdir -p "$extract_dir"
    curl -fsSL "$(build_github_archive_url "$repo_url" "$sha")" -o "$archive_file"
    tar -xzf "$archive_file" -C "$extract_dir"
  fi

  extracted_root="$(find "$extract_dir" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  if [[ -z "$extracted_root" ]]; then
    echo "Could not resolve extracted GitHub source for ${repo_url}@${sha}" >&2
    exit 1
  fi

  printf '%s\n' "$extracted_root"
}

load_sources() {
  local row
  local kind
  local arg1
  local arg2
  local source_root
  local source_label

  while IFS=$'\t' read -r kind arg1 arg2; do
    [[ -z "$kind" ]] && continue

    case "$kind" in
      github)
        source_root="$(prepare_github_source "$arg1" "$arg2")"
        source_label="github:${arg1}@${arg2}"
        SOURCE_ROOTS+=("$source_root")
        SOURCE_LABELS+=("$source_label")
        ;;
      local)
        source_root="$(expand_path "$arg1")"
        if [[ ! -d "$source_root" ]]; then
          echo "Local source root not found: $source_root" >&2
          exit 1
        fi
        source_label="local:${source_root}"
        SOURCE_ROOTS+=("$source_root")
        SOURCE_LABELS+=("$source_label")
        ;;
      *)
        echo "Unsupported source kind in $SOURCES_FILE: $kind" >&2
        exit 1
        ;;
    esac
  done < <(
    python3 - "$SOURCES_FILE" <<'PY'
import re
import sys
from pathlib import Path

config_path = Path(sys.argv[1]).resolve()
sources_root = config_path.parent

if not config_path.exists():
    print(f"Source config not found: {config_path}", file=sys.stderr)
    raise SystemExit(1)

def clean(value: str) -> str:
    value = value.strip()
    if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
        value = value[1:-1]
    return value

items = []
current = None
for raw in config_path.read_text(encoding="utf-8").splitlines():
    line = raw.split("#", 1)[0].rstrip()
    if not line.strip():
        continue
    if re.match(r"^\s*version\s*:", line) or re.match(r"^\s*sources\s*:", line):
        continue
    m = re.match(r"^\s*-\s*kind\s*:\s*(.+?)\s*$", line)
    if m:
        if current:
            items.append(current)
        current = {"kind": clean(m.group(1))}
        continue
    if current is None:
        continue
    m = re.match(r"^\s+([A-Za-z_]+)\s*:\s*(.+?)\s*$", line)
    if m:
        current[m.group(1)] = clean(m.group(2))
if current:
    items.append(current)

for item in items:
    kind = item.get("kind")
    if kind == "github":
      repo = item.get("repo") or item.get("url")
      sha = item.get("sha")
      if not repo or not sha:
          print(f"Invalid github source entry in {config_path}: {item}", file=sys.stderr)
          raise SystemExit(1)
      print(f"github\t{repo}\t{sha}")
    elif kind == "local":
      path = item.get("path")
      if not path:
          print(f"Invalid local source entry in {config_path}: {item}", file=sys.stderr)
          raise SystemExit(1)
      if path.startswith("~/"):
          resolved = str(Path.home() / path[2:])
      elif path.startswith("/"):
          resolved = path
      else:
          resolved = str((sources_root / path).resolve())
      print(f"local\t{resolved}")
    else:
      print(f"Unsupported source kind in {config_path}: {kind}", file=sys.stderr)
      raise SystemExit(1)
PY
  )
}

resolve_skill_dir() {
  local skill_name="$1"
  local i
  local candidate

  for i in "${!SOURCE_ROOTS[@]}"; do
    candidate="${SOURCE_ROOTS[$i]}/${skill_name}"
    if [[ -d "$candidate" && -f "$candidate/SKILL.md" ]]; then
      RESOLVED_SKILL_DIR="$candidate"
      RESOLVED_SOURCE_LABEL="${SOURCE_LABELS[$i]}"
      return 0
    fi
  done

  return 1
}

stage_and_package() {
  local skill_name="$1"
  local skill_dir="$2"
  local source_label="$3"

  if [[ -n "${SEEN_SKILLS[$skill_name]:-}" ]]; then
    echo "Duplicate skill name detected in manifest: $skill_name" >&2
    return 1
  fi
  SEEN_SKILLS[$skill_name]="$source_label"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "would package: $skill_name <- $source_label"
    return 0
  fi

  stage_root="$(mktemp -d)"
  staged_skill="${stage_root}/${skill_name}"
  mkdir -p "$staged_skill"

  rsync -a \
    --exclude '.git/' \
    --exclude '.DS_Store' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude 'modes/' \
    --exclude '*.skill' \
    --exclude '*.zip' \
    --exclude 'briefs/*.md' \
    "${skill_dir}/" "${staged_skill}/"

  python3 "$PACKAGER" "$staged_skill" "$OUTPUT_DIR"
  rm -rf "$stage_root"
}

require_cmd python3
require_cmd rsync
require_cmd curl
require_cmd tar

if [[ ! -f "$PACKAGER" ]]; then
  echo "Packager not found: $PACKAGER" >&2
  exit 1
fi

if [[ ! -f "$MANIFEST_FILE" ]]; then
  echo "Manifest file not found: $MANIFEST_FILE" >&2
  exit 1
fi

if [[ ! -f "$SOURCES_FILE" ]]; then
  echo "Source config not found: $SOURCES_FILE" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"
mkdir -p "$CACHE_DIR"

if [[ "$CLEAR_OUTPUT" -eq 1 && "$DRY_RUN" -eq 0 ]]; then
  find "$OUTPUT_DIR" -maxdepth 1 -type f -name '*.skill' -delete
fi

declare -A SEEN_SKILLS=()
declare -a SOURCE_ROOTS=()
declare -a SOURCE_LABELS=()
failures=0
RESOLVED_SKILL_DIR=""
RESOLVED_SOURCE_LABEL=""

load_sources

while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
  line="${raw_line%%#*}"
  line="$(trim "$line")"
  [[ -z "$line" ]] && continue

  if ! resolve_skill_dir "$line"; then
    echo "Could not resolve skill from sources: $line" >&2
    failures=$((failures + 1))
    continue
  fi

  if ! stage_and_package "$line" "$RESOLVED_SKILL_DIR" "$RESOLVED_SOURCE_LABEL"; then
    failures=$((failures + 1))
  fi
done < "$MANIFEST_FILE"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry-run complete"
elif [[ "$failures" -eq 0 ]]; then
  echo "skill sync complete: $(find "$OUTPUT_DIR" -maxdepth 1 -type f -name '*.skill' | wc -l | tr -d ' ') bundles ready in $OUTPUT_DIR"
else
  echo "skill sync finished with $failures failure(s)" >&2
  exit 1
fi
