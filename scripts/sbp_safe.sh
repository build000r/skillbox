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
  --fast                   Skip the guard subprocess entirely; read load/CPU
                           directly. Still refuses on the control plane.
  --timeout N              Hard cap on the guard subprocess (default 15s).
                           On expiry the verdict is UNKNOWN and exits 1.
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
  # Pure bash on purpose. This used to shell out to python3, which meant six
  # interpreter starts per JSON tick — the exact cost you cannot afford on the
  # loaded box this command exists to measure. Every value escaped here is a
  # number, a timestamp, a verdict token, or "?", so backslash/quote handling
  # plus a control-character guard is sufficient.
  local raw="$1"
  raw="${raw//\\/\\\\}"
  raw="${raw//\"/\\\"}"
  raw="${raw//$'\t'/ }"
  raw="${raw//$'\n'/ }"
  raw="${raw//$'\r'/ }"
  printf '"%s"' "${raw}"
}

# Run a command under a hard wall-clock cap. Returns 124 on timeout, like
# coreutils `timeout`, so callers have one code to branch on.
#
# Prefers real `timeout`/`gtimeout` (they handle process groups properly). The
# bash fallback exists because a bare macOS has neither, and a guard with no cap
# is precisely the bug this file is fixing. The fallback kills the direct child
# only: an orphaned grandchild (`uptime` on a wedged box) is left to exit on its
# own, because the goal is that THIS command returns, not that it reaps a
# process tree it did not create.
run_with_timeout() {
  local secs="$1"
  shift
  local rc=0
  if [[ -n "${TIMEOUT_BIN}" ]]; then
    "${TIMEOUT_BIN}" "${secs}" "$@" 2>&1 || rc=$?
    return "${rc}"
  fi
  local tmp pid waited limit
  tmp="$(mktemp "${TMPDIR:-/tmp}/sbp-safe.XXXXXX")"
  "$@" >"${tmp}" 2>&1 &
  pid=$!
  waited=0
  limit=$((secs * 10))
  while kill -0 "${pid}" 2>/dev/null; do
    if [[ "${waited}" -ge "${limit}" ]]; then
      kill -TERM "${pid}" 2>/dev/null || true
      sleep 0.5
      kill -KILL "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
      cat "${tmp}"
      rm -f "${tmp}"
      return 124
    fi
    sleep 0.1
    waited=$((waited + 1))
  done
  wait "${pid}" 2>/dev/null || rc=$?
  cat "${tmp}"
  rm -f "${tmp}"
  return "${rc}"
}

# Load + CPU without forking the guard (which forks `uptime`, the thing that
# wedges under load). This is what an operator falls back to by hand.
read_load_direct() {
  cpu="${SKILLBOX_SWARM_CPU_OVERRIDE:-}"
  if [[ -z "${cpu}" ]]; then
    cpu="$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 1)"
  fi
  load1="${SKILLBOX_SWARM_LOAD1_OVERRIDE:-}"
  if [[ -z "${load1}" ]]; then
    if [[ -r /proc/loadavg ]]; then
      read -r load1 _ < /proc/loadavg
    else
      load1="$(sysctl -n vm.loadavg 2>/dev/null | awk '{print $2}')"
    fi
  fi
  load1="${load1:-?}"
  cpu="${cpu:-?}"
  read -r ceiling rec <<<"$(awk -v c="${cpu}" -v l="${load1}" -v f="${factor_used}" 'BEGIN{
    ceil=f*c; head=int(c-l-1); if(head<0)head=0; printf "%.2f %d", ceil, head }')"
}

# The guard refuses local swarm work on the control plane regardless of load.
# The fast path must never turn that NO-GO into a GO, so it re-checks the same
# signals and fails closed. Deliberately conservative: matching one host too
# many costs a needless NO-GO, matching one too few would fabricate a GO.
looks_like_control_plane() {
  local host
  host="$(hostname -s 2>/dev/null || hostname 2>/dev/null || true)"
  case "${host}" in
    skillbox-portfolio-devbox | portfolio-devbox) return 0 ;;
  esac
  case "$(pwd -P)" in
    /srv/skillbox/repos | /srv/skillbox/repos/* | /srv/repos | /srv/repos/*) return 0 ;;
  esac
  return 1
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
  printf '{"ts":%s,"ok":%s,"verdict":%s,"source":%s,"load1":%s,"cpu":%s,"abort_ceiling":%s,"recommended_max_workers":%s,"factor":%s,"exit":%s}\n' \
    "$(json_escape "${ts}")" \
    "$([[ "${verdict}" == "GO" ]] && echo true || echo false)" \
    "$(json_escape "${verdict}")" \
    "$(json_escape "${source_used:-guard}")" \
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
FAST="false"
# A load guard that hangs when load is high defeats its purpose, so the guard
# subprocess gets a hard cap. 15s is far longer than the guard needs and far
# shorter than the 120s+ hangs observed on 2026-07-30.
TIMEOUT_S="${SBP_SAFE_TIMEOUT:-15}"

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
    --fast)
      FAST="true"
      shift
      ;;
    --timeout)
      TIMEOUT_S="${2:-}"
      if [[ -z "${TIMEOUT_S}" || ! "${TIMEOUT_S}" =~ ^[0-9]+$ || "${TIMEOUT_S}" -lt 1 ]]; then
        echo "sbp safe: --timeout requires a positive integer (seconds)" >&2
        exit 2
      fi
      shift 2
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

TIMEOUT_BIN=""
for candidate in timeout gtimeout; do
  if command -v "${candidate}" >/dev/null 2>&1; then
    TIMEOUT_BIN="${candidate}"
    break
  fi
done

GUARD="$(find_guard || true)"
if [[ -z "${GUARD}" && "${FAST}" != "true" ]]; then
  echo "sbp safe: swarm-load-guard.sh not found (set SBP_SAFE_GUARD or install vibing-with-ntm)" >&2
  echo "sbp safe: --fast computes the same load ceiling without the guard" >&2
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
source_used="guard"

run_once() {
  local out rc ts verdict line
  if [[ "${FAST}" == "true" ]]; then
    # No guard fork at all. Same arithmetic, cheaper inputs.
    source_used="fast"
    read_load_direct
    if looks_like_control_plane; then
      verdict="NO-GO"
      rc=1
    elif awk -v l="${load1}" -v c="${ceiling}" 'BEGIN{exit !(l>c)}'; then
      verdict="NO-GO"
      rc=1
    else
      verdict="GO"
      rc=0
    fi
  else
    set +e
    out="$(run_with_timeout "${TIMEOUT_S}" "${GUARD}" "${WORKERS}")"
    rc=$?
    set -e
    parse_guard_out "${out}"
    if [[ "${rc}" -eq 124 ]]; then
      # The guard wedged. Report that honestly and fail toward NO-GO: an
      # unknown must never be spent as a GO, and inventing a verdict from a
      # cheaper source here would hide that the control-plane refusal — which
      # only the guard knows — was never evaluated.
      source_used="guard-timeout"
      verdict="UNKNOWN"
      rc=1
      echo "sbp safe: guard exceeded ${TIMEOUT_S}s and was killed; verdict UNKNOWN (treated as NO-GO)" >&2
      echo "sbp safe: retry with --fast for a load-only reading that never forks the guard" >&2
    elif [[ "${rc}" -eq 0 ]]; then
      source_used="guard"
      verdict="GO"
    else
      source_used="guard"
      verdict="NO-GO"
      # Guard may exit non-1 on unexpected failure; still treat as NO-GO for spawn decisions.
      [[ "${rc}" -eq 1 ]] || rc=1
    fi
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
