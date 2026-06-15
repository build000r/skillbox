#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${SBP_SEND_LATER_STATE_DIR:-/srv/skillbox/state/ntm-send-later}"
LOG_DIR="${SBP_SEND_LATER_LOG_DIR:-/srv/skillbox/state/logs}"
SBP_BIN="${SBP_SEND_LATER_SBP_BIN:-$(command -v sbp 2>/dev/null || true)}"
if [[ -z "${SBP_BIN}" ]]; then
  SBP_BIN="/home/skillbox/.local/bin/sbp"
fi
CRON_MARKER="sbp-send-later-wrapper"
CRON_LINE="* * * * * ${SBP_BIN} send-later run-pending >> ${LOG_DIR}/ntm-send-later.cron.log 2>&1 # ${CRON_MARKER}"

# --when-waiting state detection. A pane is treated as "waiting" only when its
# captured tail is STABLE across ticks (not streaming), matches an idle footer,
# and does NOT match a busy/working indicator. Tunable per-job via --idle-regex
# / --busy-regex, or globally via the env vars below.
DEFAULT_IDLE_RE="${SBP_SEND_LATER_IDLE_RE:-Shift\\+Tab:mode|Ctrl\\+\\.:shortcuts|Grok Composer|\\? for shortcuts}"
DEFAULT_BUSY_RE="${SBP_SEND_LATER_BUSY_RE:-esc to interrupt|Esc to interrupt|esc to cancel|ctrl-c to interrupt|Ctrl\\+c:cancel|Ctrl\\+Enter:interject|Waiting…|Thinking…|Generating…|tokens/s|⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏}"
CAPTURE_LINES="${SBP_SEND_LATER_CAPTURE_LINES:-40}"

usage() {
  cat <<'EOF'
Usage:
  sbp send-later
  sbp send-later list
  sbp send-later install-cron
  sbp send-later schedule --minutes N --id ID --session SESSION --pane PANE --message TEXT [gate flags]
  sbp send-later schedule --minutes N --id ID --target TMUX_TARGET --key KEY [--key KEY ...] [--key-delay SECONDS] [gate flags]
  sbp send-later run-pending

Gate / recurring flags (optional):
  --recurring            Keep re-evaluating every cron tick instead of firing once.
  --when-waiting         Only fire when the target pane looks idle/waiting (stable
                         tail + idle footer + no busy indicator), never mid-run.
  --cooldown-minutes N   Minimum minutes between sends (default 0).
  --renudge-minutes N    Re-send the SAME idle screen after N minutes (default 0 = never).
  --idle-regex RE        Override the "waiting" footer signature (extended regex).
  --busy-regex RE        Override the "running" signature (extended regex).

Examples:
  # One-shot, fire after 5h regardless of state (rate-limit window):
  sbp send-later schedule --minutes 300 --id my-job --session devbox-1 --pane 0 --message continue

  # Recurring auto-continue: every minute, type "continue" ONLY when grok pane 0 is idle:
  sbp send-later schedule --minutes 0 --id grok-continue --target devbox-1:0.0 \
    --key continue --key Enter --recurring --when-waiting

  sbp send-later schedule --minutes 300 --id my-rate-limit-job --target devbox-1:0.0 --key 1 --key Enter --key continue --key Enter
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 2
}

sanitize_id() {
  local id="$1"
  [[ "$id" =~ ^[A-Za-z0-9_.-]+$ ]] || die "id must match [A-Za-z0-9_.-]+"
  printf '%s' "$id"
}

require_value() {
  local flag="$1" count="$2"
  [[ "$count" -ge 2 ]] || die "$flag requires a value"
}

validate_key_delay() {
  local delay="$1"
  [[ "$delay" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "--key-delay must be a non-negative number"
}

b64() {
  printf '%s' "$1" | base64 -w0
}

unb64() {
  printf '%s' "$1" | base64 -d
}

install_cron() {
  mkdir -p "$STATE_DIR" "$LOG_DIR"
  local tmp
  tmp="$(mktemp)"
  crontab -l 2>/dev/null | grep -v "$CRON_MARKER" > "$tmp" || true
  printf '%s\n' "$CRON_LINE" >> "$tmp"
  crontab "$tmp"
  rm -f "$tmp"
  echo "installed cron: $CRON_LINE"
}

schedule_job() {
  local minutes="" id="" session="" pane="" message="" target="" key_delay="1"
  local recurring="false" when_waiting="false" cooldown_min="0" renudge_min="0"
  local idle_re="" busy_re=""
  local keys=()

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --minutes) require_value "$1" "$#"; minutes="$2"; shift 2 ;;
      --id) require_value "$1" "$#"; id="$(sanitize_id "$2")"; shift 2 ;;
      --session) require_value "$1" "$#"; session="$2"; shift 2 ;;
      --pane) require_value "$1" "$#"; pane="$2"; shift 2 ;;
      --message) require_value "$1" "$#"; message="$2"; shift 2 ;;
      --target) require_value "$1" "$#"; target="$2"; shift 2 ;;
      --key) require_value "$1" "$#"; keys+=("$2"); shift 2 ;;
      --key-delay) require_value "$1" "$#"; key_delay="$2"; shift 2 ;;
      --recurring) recurring="true"; shift 1 ;;
      --when-waiting) when_waiting="true"; shift 1 ;;
      --cooldown-minutes) require_value "$1" "$#"; cooldown_min="$2"; shift 2 ;;
      --renudge-minutes) require_value "$1" "$#"; renudge_min="$2"; shift 2 ;;
      --idle-regex) require_value "$1" "$#"; idle_re="$2"; shift 2 ;;
      --busy-regex) require_value "$1" "$#"; busy_re="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown schedule arg: $1" ;;
    esac
  done

  [[ "$minutes" =~ ^[0-9]+$ ]] || die "--minutes must be a non-negative integer"
  [[ -n "$id" ]] || die "--id is required"
  validate_key_delay "$key_delay"
  [[ "$cooldown_min" =~ ^[0-9]+$ ]] || die "--cooldown-minutes must be a non-negative integer"
  [[ "$renudge_min" =~ ^[0-9]+$ ]] || die "--renudge-minutes must be a non-negative integer"

  local mode=""
  if [[ -n "$message" ]]; then
    [[ -n "$session" ]] || die "--session is required with --message"
    [[ -n "$pane" ]] || die "--pane is required with --message"
    [[ "${#keys[@]}" -eq 0 ]] || die "use either --message/--session/--pane or --target/--key, not both"
    [[ -z "$target" ]] || die "use either --message/--session/--pane or --target/--key, not both"
    mode="ntm_send"
  elif [[ "${#keys[@]}" -gt 0 ]]; then
    [[ -n "$target" ]] || die "--target is required with --key"
    [[ -z "$session" && -z "$pane" ]] || die "use either --message/--session/--pane or --target/--key, not both"
    mode="tmux_keys"
  else
    die "provide either --message or at least one --key"
  fi

  mkdir -p "$STATE_DIR" "$LOG_DIR"
  install_cron >/dev/null

  local now due due_utc job keys_joined
  now="$(date -u +%s)"
  due=$((now + minutes * 60))
  due_utc="$(date -u -d "@$due" +%Y-%m-%dT%H:%M:%SZ)"
  job="$STATE_DIR/$id.env"
  local replaced="false"
  if [[ -f "$job" ]]; then
    replaced="true"
  fi
  rm -f "$STATE_DIR/$id.done" "$STATE_DIR/$id.last" "$STATE_DIR/$id.sentfp" "$STATE_DIR/$id.lastsent"

  keys_joined=""
  if [[ "${#keys[@]}" -gt 0 ]]; then
    keys_joined="$(printf '%s\n' "${keys[@]}")"
  fi

  cat > "$job" <<EOF
ID='$id'
MODE='$mode'
DUE_EPOCH='$due'
DUE_UTC='$due_utc'
SESSION_B64='$(b64 "$session")'
PANE_B64='$(b64 "$pane")'
MESSAGE_B64='$(b64 "$message")'
TARGET_B64='$(b64 "$target")'
KEYS_B64='$(b64 "$keys_joined")'
KEY_DELAY='$key_delay'
RECURRING='$recurring'
WHEN_WAITING='$when_waiting'
COOLDOWN_MIN='$cooldown_min'
RENUDGE_MIN='$renudge_min'
IDLE_RE_B64='$(b64 "$idle_re")'
BUSY_RE_B64='$(b64 "$busy_re")'
EOF

  echo "scheduled: id=$id due_utc=$due_utc mode=$mode recurring=$recurring when_waiting=$when_waiting replaced=$replaced"
  echo "state: job=$job log=$STATE_DIR/$id.log"
  echo "next: sbp send-later list"
}

send_tmux_key() {
  local target="$1" key="$2"
  case "$key" in
    Enter|Escape|Esc|C-c|C-u|C-m|Tab|Space|BSpace|Delete|Up|Down|Left|Right)
      tmux send-keys -t "$target" "$key"
      ;;
    *)
      tmux send-keys -t "$target" -l "$key"
      ;;
  esac
}

# Resolve the tmux pane target a job should inspect for --when-waiting gating.
gate_target() {
  if [[ "$MODE" == "tmux_keys" ]]; then
    unb64 "$TARGET_B64"
    return 0
  fi
  local s p win
  s="$(unb64 "$SESSION_B64")"
  p="$(unb64 "$PANE_B64")"
  win="$(tmux list-windows -t "=$s:" -F '#{window_index}' 2>/dev/null | head -n1)"
  [[ -n "$win" ]] || win=0
  printf '=%s:%s.%s' "$s" "$win" "$p"
}

# Capture the pane tail, normalized so a truly-idle screen is byte-stable across
# ticks (strip the cursor block and trailing whitespace).
capture_norm() {
  local target="$1"
  tmux capture-pane -p -t "$target" -S "-${CAPTURE_LINES}" 2>/dev/null \
    | sed -e 's/█//g' -e 's/[[:space:]]*$//'
}

# Classify a capture as waiting|running|unknown.
#   running  -> a busy indicator is present, OR the tail changed since last tick
#   waiting  -> stable since last tick AND an idle footer is present
#   unknown  -> stable but no recognizable footer (do not send)
state_of_capture() {
  local cur="$1" prev="$2" idle_re="$3" busy_re="$4"
  if printf '%s\n' "$cur" | grep -Eq -- "$busy_re" 2>/dev/null; then
    printf 'running'; return 0
  fi
  if [[ "$cur" != "$prev" ]]; then
    printf 'running'; return 0
  fi
  if printf '%s\n' "$cur" | grep -Eq -- "$idle_re" 2>/dev/null; then
    printf 'waiting'; return 0
  fi
  printf 'unknown'
}

run_job() {
  local job="$1"
  # shellcheck disable=SC1090
  source "$job"

  # Backward-compatible defaults for jobs scheduled before these fields existed.
  local recurring="${RECURRING:-false}"
  local when_waiting="${WHEN_WAITING:-false}"
  local cooldown_min="${COOLDOWN_MIN:-0}"
  local renudge_min="${RENUDGE_MIN:-0}"
  local idle_re busy_re
  idle_re="$(unb64 "${IDLE_RE_B64:-}")"; [[ -n "$idle_re" ]] || idle_re="$DEFAULT_IDLE_RE"
  busy_re="$(unb64 "${BUSY_RE_B64:-}")"; [[ -n "$busy_re" ]] || busy_re="$DEFAULT_BUSY_RE"

  local now done_file log_file lock_file last_file fp_file sent_file
  now="$(date -u +%s)"
  [[ "$now" -ge "$DUE_EPOCH" ]] || return 0

  done_file="$STATE_DIR/$ID.done"
  log_file="$STATE_DIR/$ID.log"
  lock_file="$STATE_DIR/$ID.lock"
  last_file="$STATE_DIR/$ID.last"
  fp_file="$STATE_DIR/$ID.sentfp"
  sent_file="$STATE_DIR/$ID.lastsent"

  # One-shot jobs stop after firing; recurring jobs keep re-evaluating each tick.
  if [[ "$recurring" != "true" ]]; then
    [[ ! -f "$done_file" ]] || return 0
  fi

  exec 9>"$lock_file"
  if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) id=$ID already_running" >> "$log_file"
    return 0
  fi

  local fp=""
  if [[ "$when_waiting" == "true" ]]; then
    local target cur prev state last_fp
    target="$(gate_target)"
    if [[ -z "$target" ]]; then
      echo "$(date -u +%FT%TZ) id=$ID gate=no-target -> skip" >> "$log_file"
      return 0
    fi
    cur="$(capture_norm "$target")"
    prev=""
    [[ -f "$last_file" ]] && prev="$(cat "$last_file")"
    printf '%s' "$cur" > "$last_file"

    state="$(state_of_capture "$cur" "$prev" "$idle_re" "$busy_re")"
    if [[ "$state" != "waiting" ]]; then
      echo "$(date -u +%FT%TZ) id=$ID gate=$state target=$target -> skip" >> "$log_file"
      return 0
    fi

    fp="$(printf '%s' "$cur" | sha1sum | awk '{print $1}')"
    last_fp=""
    [[ -f "$fp_file" ]] && last_fp="$(cat "$fp_file" 2>/dev/null || true)"
    if [[ "$fp" == "$last_fp" ]]; then
      # Same idle screen we already nudged. Re-nudge only if its window elapsed.
      local last_sent=0
      [[ -f "$sent_file" ]] && last_sent="$(cat "$sent_file" 2>/dev/null || echo 0)"
      if [[ "$renudge_min" -le 0 ]] || (( now - last_sent < renudge_min * 60 )); then
        echo "$(date -u +%FT%TZ) id=$ID gate=waiting fp=dup -> skip" >> "$log_file"
        return 0
      fi
    fi
    echo "$(date -u +%FT%TZ) id=$ID gate=waiting fp=$fp -> fire" >> "$log_file"
  fi

  # Optional cooldown between sends, independent of screen fingerprint.
  if [[ "$cooldown_min" -gt 0 && -f "$sent_file" ]]; then
    local ls; ls="$(cat "$sent_file" 2>/dev/null || echo 0)"
    if (( now - ls < cooldown_min * 60 )); then
      echo "$(date -u +%FT%TZ) id=$ID cooldown active -> skip" >> "$log_file"
      return 0
    fi
  fi

  local status=0
  echo "$(date -u +%FT%TZ) id=$ID firing mode=$MODE due=$DUE_UTC recurring=$recurring when_waiting=$when_waiting" >> "$log_file"
  set +e
  if [[ "$MODE" == "ntm_send" ]]; then
    ntm send "$(unb64 "$SESSION_B64")" \
      --pane="$(unb64 "$PANE_B64")" \
      --no-cass-check \
      --force-non-interactive \
      "$(unb64 "$MESSAGE_B64")" >> "$log_file" 2>&1
    status=$?
  elif [[ "$MODE" == "tmux_keys" ]]; then
    local target keys_text key
    target="$(unb64 "$TARGET_B64")"
    keys_text="$(unb64 "$KEYS_B64")"
    while IFS= read -r key; do
      [[ -n "$key" ]] || continue
      send_tmux_key "$target" "$key" >> "$log_file" 2>&1
      sleep "$KEY_DELAY"
    done <<< "$keys_text"
    status=$?
  else
    echo "unknown mode: $MODE" >> "$log_file"
    status=2
  fi
  set -e

  echo "$(date -u +%FT%TZ) id=$ID status=$status" >> "$log_file"
  if [[ "$status" -eq 0 ]]; then
    date -u +%s > "$sent_file"
    [[ -n "$fp" ]] && printf '%s' "$fp" > "$fp_file"
    if [[ "$recurring" != "true" ]]; then
      {
        echo "completed_at=$(date -u +%FT%TZ)"
        echo "job=$job"
        echo "log=$log_file"
      } > "$done_file"
    fi
  fi
}

run_pending() {
  mkdir -p "$STATE_DIR" "$LOG_DIR"
  local job
  shopt -s nullglob
  for job in "$STATE_DIR"/*.env; do
    run_job "$job"
  done
}

list_jobs() {
  mkdir -p "$STATE_DIR" "$LOG_DIR"
  local job id due mode status
  shopt -s nullglob
  local jobs=("$STATE_DIR"/*.env)
  echo "jobs: ${#jobs[@]}"
  if [[ "${#jobs[@]}" -eq 0 ]]; then
    echo "next: sbp send-later schedule --minutes N --id ID --session SESSION --pane PANE --message TEXT"
    return 0
  fi
  for job in "${jobs[@]}"; do
    # shellcheck disable=SC1090
    source "$job"
    id="$ID"
    due="$DUE_UTC"
    mode="$MODE"
    status="pending"
    [[ "${RECURRING:-false}" == "true" ]] && status="recurring"
    [[ -f "$STATE_DIR/$id.done" ]] && status="done"
    echo "- id=$id status=$status due_utc=$due mode=$mode recurring=${RECURRING:-false} gate=${WHEN_WAITING:-false} job=$job"
  done
  echo "next: sbp send-later run-pending"
}

main() {
  local cmd="${1:-}"
  shift || true
  case "$cmd" in
    install-cron) install_cron ;;
    schedule) schedule_job "$@" ;;
    run-pending) run_pending ;;
    list) list_jobs ;;
    -h|--help) usage ;;
    "") list_jobs ;;
    *) die "unknown command: $cmd" ;;
  esac
}

main "$@"
