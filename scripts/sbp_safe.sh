#!/usr/bin/env bash
# sbp safe — watch swarm load headroom (wraps vibing-with-ntm swarm-load-guard).
#
# One-shot:
#   sbp safe
#   sbp safe --json
#   sbp safe --workers 2 --factor 0.40
#
# Watch tick (seconds):
#   sbp safe 10
#   sbp safe 30s --factor 0.40
#   sbp safe 10 --count 6
#   sbp safe 10 --log /srv/skillbox/artifacts/runs/swarm-load-tick.log
#
# Exit codes (one-shot / last watch tick):
#   0  GO   — safe to launch more agents (up to recommended_max_workers)
#   1  NO-GO — load exceeds abort ceiling
#   2  usage / missing guard
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  sbp safe                 One-shot load gate (GO / NO-GO)
  sbp safe SECONDS         Tick every SECONDS (e.g. 10, 30s) until Ctrl-C
  sbp safe SECONDS --count N
                           Tick N times then exit with last verdict

Options:
  --once                   Force one-shot (default when SECONDS omitted)
  --count N                Stop after N ticks (watch mode)
  --workers N              Requested workers (warn if above recommended)
  --factor F               Load abort ceiling as fraction of CPUs
                           (sets SKILLBOX_SWARM_LOAD_FACTOR; default 0.75)
  --log PATH               Also append each tick line to PATH
  --json                   Machine-readable: one JSON object (once) or NDJSON (watch)
  -h, --help               Show this help

Output (human):
  2026-07-15T17:00:01Z  GO     load1=3.20  cpu=8  ceiling=6.00  rec=3  factor=0.75

Semantics come from vibing-with-ntm scripts/swarm-load-guard.sh:
  abort_ceiling = factor × cores
  recommended_max_workers ≈ max(0, cores − load1 − 1)
  GO only when load1 ≤ abort_ceiling

Env:
  SBP_SAFE_GUARD                 Override path to swarm-load-guard.sh
  SKILLBOX_SWARM_LOAD_FACTOR     Default load factor when --factor omitted
  SKILLBOX_OPERATOR_REPOS_ROOT   Used to locate skills-private
EOF
}

iso_now() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

find_guard() {
  local candidate=""
  local candidates=(
    "${SBP_SAFE_GUARD:-}"
    "${SKILLBOX_OPERATOR_REPOS_ROOT:+${SKILLBOX_OPERATOR_REPOS_ROOT}/skills-private/vibing-with-ntm/scripts/swarm-load-guard.sh}"
    /srv/skillbox/repos/skills-private/vibing-with-ntm/scripts/swarm-load-guard.sh
    /srv/repos/skills-private/vibing-with-ntm/scripts/swarm-load-guard.sh
    "${HOME}/repos/skills-private/vibing-with-ntm/scripts/swarm-load-guard.sh"
  )
  for candidate in "${candidates[@]}"; do
    [[ -n "${candidate}" && -f "${candidate}" && -x "${candidate}" ]] || continue
    printf '%s\n' "${candidate}"
    return 0
  done
  return 1
}

parse_seconds() {
  # Accept: 10 | 10s | 10S
  local raw="$1"
  if [[ "${raw}" =~ ^([0-9]+)[sS]?$ ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
    return 0
  fi
  return 1
}

json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
}

# Parse guard stderr into shell vars: load1 cpu ceiling rec factor_used
# Also sets verdict from exit code argument.
parse_guard_out() {
  local text="$1"
  load1="$(printf '%s\n' "${text}" | sed -nE 's/.*load1=([0-9.]+).*/\1/p' | head -1)"
  cpu="$(printf '%s\n' "${text}" | sed -nE 's/.*cpu=([0-9]+).*/\1/p' | head -1)"
  ceiling="$(printf '%s\n' "${text}" | sed -nE 's/.*abort_ceiling=([0-9.]+).*/\1/p' | head -1)"
  rec="$(printf '%s\n' "${text}" | sed -nE 's/.*recommended_max_workers=([0-9]+).*/\1/p' | head -1)"
  load1="${load1:-?}"
  cpu="${cpu:-?}"
  ceiling="${ceiling:-?}"
  rec="${rec:-?}"
}

emit_human() {
  local ts="$1" verdict="$2"
  # Fixed-width verdict for easy scanning while watching.
  printf '%s  %-5s  load1=%s  cpu=%s  ceiling=%s  rec=%s  factor=%s\n' \
    "${ts}" "${verdict}" "${load1}" "${cpu}" "${ceiling}" "${rec}" "${factor_used}"
}

emit_json() {
  local ts="$1" verdict="$2" rc="$3"
  printf '{"ts":%s,"ok":%s,"verdict":%s,"load1":%s,"cpu":%s,"abort_ceiling":%s,"recommended_max_workers":%s,"factor":%s,"exit":%s}\n' \
    "$(json_escape "${ts}")" \
    "$([[ "${verdict}" == "GO" ]] && echo true || echo false)" \
    "$(json_escape "${verdict}")" \
    "$(json_escape "${load1}")" \
    "$(json_escape "${cpu}")" \
    "$(json_escape "${ceiling}")" \
    "$(json_escape "${rec}")" \
    "$(json_escape "${factor_used}")" \
    "${rc}"
}

INTERVAL=""
ONCE="false"
COUNT=""
WORKERS="0"
FACTOR="${SKILLBOX_SWARM_LOAD_FACTOR:-}"
LOG_PATH=""
JSON="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help|help)
      usage
      exit 0
      ;;
    --once)
      ONCE="true"
      shift
      ;;
    --count)
      COUNT="${2:-}"
      if [[ -z "${COUNT}" || ! "${COUNT}" =~ ^[0-9]+$ || "${COUNT}" -lt 1 ]]; then
        echo "sbp safe: --count requires a positive integer" >&2
        exit 2
      fi
      shift 2
      ;;
    --workers)
      WORKERS="${2:-}"
      if [[ -z "${WORKERS}" || ! "${WORKERS}" =~ ^[0-9]+$ ]]; then
        echo "sbp safe: --workers requires a non-negative integer" >&2
        exit 2
      fi
      shift 2
      ;;
    --factor)
      FACTOR="${2:-}"
      if [[ -z "${FACTOR}" || ! "${FACTOR}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        echo "sbp safe: --factor requires a number (e.g. 0.40)" >&2
        exit 2
      fi
      shift 2
      ;;
    --log)
      LOG_PATH="${2:-}"
      if [[ -z "${LOG_PATH}" ]]; then
        echo "sbp safe: --log requires a path" >&2
        exit 2
      fi
      shift 2
      ;;
    --json)
      JSON="true"
      shift
      ;;
    --format)
      # sbp may forward --format json via append_json_flag default path
      if [[ "${2:-}" == "json" ]]; then
        JSON="true"
        shift 2
      else
        echo "sbp safe: unsupported --format ${2:-}" >&2
        exit 2
      fi
      ;;
    -*)
      echo "sbp safe: unknown flag: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [[ -n "${INTERVAL}" ]]; then
        echo "sbp safe: unexpected argument: $1" >&2
        usage >&2
        exit 2
      fi
      if ! INTERVAL="$(parse_seconds "$1")"; then
        echo "sbp safe: interval must be seconds (e.g. 10 or 30s), got: $1" >&2
        exit 2
      fi
      if [[ "${INTERVAL}" -lt 1 ]]; then
        echo "sbp safe: interval must be ≥ 1 second" >&2
        exit 2
      fi
      shift
      continue
      ;;
  esac
done

if [[ "${ONCE}" == "true" ]]; then
  INTERVAL=""
fi

if [[ -z "${INTERVAL}" && -n "${COUNT}" ]]; then
  echo "sbp safe: --count requires a tick interval (e.g. sbp safe 10 --count 5)" >&2
  exit 2
fi

GUARD="$(find_guard || true)"
if [[ -z "${GUARD}" ]]; then
  echo "sbp safe: swarm-load-guard.sh not found (set SBP_SAFE_GUARD or install vibing-with-ntm)" >&2
  exit 2
fi

if [[ -n "${FACTOR}" ]]; then
  export SKILLBOX_SWARM_LOAD_FACTOR="${FACTOR}"
fi
factor_used="${SKILLBOX_SWARM_LOAD_FACTOR:-0.75}"

if [[ -n "${LOG_PATH}" ]]; then
  mkdir -p "$(dirname "${LOG_PATH}")"
fi

last_rc=0
ticks=0

run_once() {
  local out rc ts verdict line
  set +e
  out="$("${GUARD}" "${WORKERS}" 2>&1)"
  rc=$?
  set -e
  parse_guard_out "${out}"
  if [[ "${rc}" -eq 0 ]]; then
    verdict="GO"
  else
    verdict="NO-GO"
    # Guard may exit non-1 on unexpected failure; still treat as NO-GO for spawn decisions.
    [[ "${rc}" -eq 1 ]] || rc=1
  fi
  ts="$(iso_now)"
  if [[ "${JSON}" == "true" ]]; then
    line="$(emit_json "${ts}" "${verdict}" "${rc}")"
  else
    line="$(emit_human "${ts}" "${verdict}")"
  fi
  printf '%s\n' "${line}"
  if [[ -n "${LOG_PATH}" ]]; then
    printf '%s\n' "${line}" >> "${LOG_PATH}"
  fi
  last_rc="${rc}"
  return "${rc}"
}

if [[ -z "${INTERVAL}" ]]; then
  run_once || true
  exit "${last_rc}"
fi

# Watch mode: Ctrl-C should stop cleanly without a traceback-ish nonzero if mid-sleep.
trap 'exit 0' INT TERM

while true; do
  run_once || true
  ticks=$((ticks + 1))
  if [[ -n "${COUNT}" && "${ticks}" -ge "${COUNT}" ]]; then
    exit "${last_rc}"
  fi
  sleep "${INTERVAL}"
done
