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
  sbp send-later                       Show scheduled jobs (home view)
  sbp send-later panes [--json]        List live tmux panes you can target
  sbp send-later new                   Interactive scheduler (pick pane, when, message)
  sbp send-later list                  List scheduled jobs
  sbp send-later cancel ID             Remove a scheduled job
  sbp send-later install-cron          Install the per-minute run-pending tick
  sbp send-later run-pending           Fire any due jobs (called by cron)

  sbp send-later schedule --to <#|target|name> --message TEXT [--in 5h|--at 9am] [gate flags]
  sbp send-later schedule --target SESS:WIN.PANE --key KEY [--key KEY ...] [--key-delay S] [when] [gate flags]
  sbp send-later schedule --session S --pane P --message TEXT [when] [gate flags]

When (default = now / next tick):
  --in DURATION          90s | 30m | 5h | 2d
  --at TIME              "9am" | "14:30" | "tomorrow 09:00"   (GNU date expressions)
  --minutes N            fire N minutes from now

Targeting:
  --to <#|target|name>   pick by panes-list number, session:win.pane, or fuzzy agent/title
  --force                schedule even if the target pane is not live yet
  --id ID                job id (auto-generated from target + time if omitted)

Gate / recurring flags (optional):
  --recurring            Keep re-evaluating every cron tick instead of firing once.
  --when-waiting         Only fire when the target pane looks idle/waiting (stable
                         tail + idle footer + no busy indicator), never mid-run.
  --cooldown-minutes N   Minimum minutes between sends (default 0).
  --renudge-minutes N    Re-send the SAME idle screen after N minutes (default 0 = never).
  --idle-regex RE        Override the "waiting" footer signature (extended regex).
  --busy-regex RE        Override the "running" signature (extended regex).

Examples:
  # Easiest: pick a pane and answer a few prompts
  sbp send-later new

  # One-shot, fire in 5h into pane #3 from `sbp send-later panes`:
  sbp send-later schedule --to 3 --in 5h --message "continue"

  # Auto-continue: nudge a pane ONLY when it goes idle, forever:
  sbp send-later schedule --to grok --message continue --recurring --when-waiting

  # Raw keys (advanced) at a wall-clock time:
  sbp send-later schedule --target devbox-1:0.0 --key 1 --key Enter --key continue --key Enter --at "tomorrow 09:00"
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

have_tty() {
  [[ -t 0 && -t 1 ]]
}

# Emit one TSV row per live tmux pane: idx, target, agent, state, title, here.
# Default is instant (tmux only): AGENT is the pane's current command, STATE blank.
# Pass "rich" as $1 to enrich AGENT/STATE from `ntm --robot-snapshot` — that call
# can be slow (seconds), so it is opt-in and hard-capped by `timeout`.
list_panes_tsv() {
  local rich="${1:-}"
  local me="${TMUX_PANE:-}"
  declare -A NTM_TYPE NTM_STATE
  if [[ "$rich" == "rich" ]] && command -v ntm >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
    local k t s
    while IFS=$'\t' read -r k t s; do
      [[ -n "$k" ]] || continue
      NTM_TYPE["$k"]="$t"
      NTM_STATE["$k"]="$s"
    done < <(timeout 8 ntm --robot-snapshot --robot-format=json 2>/dev/null \
      | jq -r '.sessions[]? | .name as $s | (((.agents // "[]") | fromjson?) // []) | .[]? | "\($s):\(.pane)\t\(.type // "")\t\(.state // "")"' 2>/dev/null || true)
  fi
  local fmt idx=0 target pane_id active cmd title agent state here
  fmt=$'#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}\t#{pane_active}\t#{pane_current_command}\t#{pane_title}'
  while IFS=$'\t' read -r target pane_id active cmd title; do
    [[ -n "$target" ]] || continue
    idx=$((idx + 1))
    agent="${NTM_TYPE[$target]:-$cmd}"
    state="${NTM_STATE[$target]:-}"
    here=""
    [[ -n "$me" && "$pane_id" == "$me" ]] && here="here"
    printf '%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\n' "$idx" "$target" "$agent" "$state" "$title" "$here"
  done < <(tmux list-panes -a -F "$fmt" 2>/dev/null || true)
}

# Show the pane inventory: a human table, or --json for agents/scripts.
# --rich adds live agent type + busy/idle state via ntm (slower).
cmd_panes() {
  local json="false" rich=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) json="true" ;;
      # The sbp wrapper normalizes --json to "--format json"; accept both.
      --format) [[ "${2:-}" == "json" ]] || die "panes: --format only supports json"; json="true"; shift ;;
      --rich) rich="rich" ;;
      *) die "unknown panes arg: $1 (use --json and/or --rich)" ;;
    esac
    shift
  done
  local rows; rows="$(list_panes_tsv "$rich")"
  if [[ "$json" == "true" ]]; then
    if [[ -z "$rows" ]]; then echo "[]"; return 0; fi
    printf '%s\n' "$rows" | jq -R -s '
      split("\n") | map(select(length > 0)) | map(split("\u001f"))
      | map({idx:(.[0]|tonumber), target:.[1], agent:.[2], state:.[3], title:.[4], here:(.[5]=="here")})'
    return 0
  fi
  if [[ -z "$rows" ]]; then
    echo "0 panes (no tmux server on this box)"
    return 0
  fi
  echo "Live panes you can target:"
  printf '  %-3s %-15s %-8s %-7s %s\n' "#" "TARGET" "AGENT" "STATE" "TITLE"
  local idx target agent state title here mark
  while IFS=$'\x1f' read -r idx target agent state title here; do
    mark=""; [[ "$here" == "here" ]] && mark="   <- you are here"
    printf '  %-3s %-15s %-8s %-7s %s%s\n' "$idx" "$target" "${agent:-?}" "${state:-—}" "$title" "$mark"
  done <<< "$rows"
  echo
  [[ "$rich" == "rich" ]] || echo '(AGENT = process; add --rich for live agent type + busy/idle state)'
  echo 'schedule:    sbp send-later schedule --to <#|target|name> --in 5h --message "continue"'
  echo 'interactive: sbp send-later new'
}

# Resolve a --to value (row number | session:win.pane | fuzzy agent/title) to a
# canonical tmux target. Dies with an actionable message on miss/ambiguity.
resolve_target() {
  local val="$1" rows idx target agent state title here
  rows="$(list_panes_tsv)"
  if [[ "$val" =~ ^[0-9]+$ ]]; then
    while IFS=$'\x1f' read -r idx target agent state title here; do
      [[ "$idx" == "$val" ]] && { printf '%s' "$target"; return 0; }
    done <<< "$rows"
    die "no pane #$val; run: sbp send-later panes"
  fi
  if [[ "$val" == *:* ]]; then
    printf '%s' "$val"; return 0
  fi
  local matches=()
  while IFS=$'\x1f' read -r idx target agent state title here; do
    [[ -n "$target" ]] || continue
    if [[ "${target,,}" == *"${val,,}"* || "${agent,,}" == *"${val,,}"* || "${title,,}" == *"${val,,}"* ]]; then
      matches+=("$target")
    fi
  done <<< "$rows"
  case "${#matches[@]}" in
    1) printf '%s' "${matches[0]}"; return 0 ;;
    0) die "no pane matches '$val'; run: sbp send-later panes" ;;
    *) die "'$val' matches ${#matches[@]} panes: ${matches[*]} — be more specific (use # or session:win.pane)" ;;
  esac
}

# True if TARGET resolves to a live tmux pane right now.
target_exists() {
  local target="$1" pid
  pid="$(tmux display-message -p -t "$target" '#{pane_id}' 2>/dev/null || true)"
  [[ -n "$pid" ]]
}

# Interactive scheduler for humans. Refuses to run without a TTY so agents/cron
# always take the flag path instead of blocking on a prompt.
wizard_new() {
  have_tty || die "interactive wizard needs a terminal. Use: sbp send-later schedule --to <#|target> --in 5h --message \"...\""
  cmd_panes
  local sel when msg gate recur target when_label
  read -r -p $'\nTarget? [#|session:win.pane|name] > ' sel
  [[ -n "$sel" ]] || die "no target chosen"
  target="$(resolve_target "$sel")"
  read -r -p 'When?     [e.g. 5h, 30m, 9am, now] > ' when
  [[ -n "$when" ]] || when="now"
  read -r -p 'Message?  > ' msg
  [[ -n "$msg" ]] || die "no message"
  read -r -p 'Only when the pane is idle? [y/N] > ' gate
  read -r -p 'Repeat (recurring)?        [y/N] > ' recur

  local when_flag=()
  case "$when" in
    now)          when_flag=(--minutes 0); when_label="now" ;;
    *[0-9][smhd]) when_flag=(--in "$when"); when_label="in $when" ;;
    *)            when_flag=(--at "$when"); when_label="at $when" ;;
  esac
  local extra=() gate_label="" recur_label=""
  if [[ "$gate" =~ ^[Yy] ]]; then extra+=(--when-waiting); gate_label=" (only when idle)"; fi
  if [[ "$recur" =~ ^[Yy] ]]; then extra+=(--recurring); recur_label=" (recurring)"; fi

  echo
  echo "> Will send \"$msg\" to $target $when_label$gate_label$recur_label"
  local ok
  read -r -p 'Confirm? [Y/n] > ' ok
  [[ -z "$ok" || "$ok" =~ ^[Yy] ]] || { echo "cancelled"; return 0; }
  echo
  local args=(--to "$target" "${when_flag[@]}" --message "$msg")
  [[ "${#extra[@]}" -gt 0 ]] && args+=("${extra[@]}")
  schedule_job "${args[@]}"
}

schedule_job() {
  local minutes="" in_spec="" at_spec="" id="" session="" pane="" message="" target="" to="" key_delay="1"
  local recurring="false" when_waiting="false" cooldown_min="0" renudge_min="0"
  local idle_re="" busy_re="" force="false"
  local keys=()

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --minutes) require_value "$1" "$#"; minutes="$2"; shift 2 ;;
      --in) require_value "$1" "$#"; in_spec="$2"; shift 2 ;;
      --at) require_value "$1" "$#"; at_spec="$2"; shift 2 ;;
      --id) require_value "$1" "$#"; id="$(sanitize_id "$2")"; shift 2 ;;
      --session) require_value "$1" "$#"; session="$2"; shift 2 ;;
      --pane) require_value "$1" "$#"; pane="$2"; shift 2 ;;
      --message) require_value "$1" "$#"; message="$2"; shift 2 ;;
      --target) require_value "$1" "$#"; target="$2"; shift 2 ;;
      --to) require_value "$1" "$#"; to="$2"; shift 2 ;;
      --key) require_value "$1" "$#"; keys+=("$2"); shift 2 ;;
      --key-delay) require_value "$1" "$#"; key_delay="$2"; shift 2 ;;
      --recurring) recurring="true"; shift 1 ;;
      --when-waiting) when_waiting="true"; shift 1 ;;
      --cooldown-minutes) require_value "$1" "$#"; cooldown_min="$2"; shift 2 ;;
      --renudge-minutes) require_value "$1" "$#"; renudge_min="$2"; shift 2 ;;
      --idle-regex) require_value "$1" "$#"; idle_re="$2"; shift 2 ;;
      --busy-regex) require_value "$1" "$#"; busy_re="$2"; shift 2 ;;
      --force) force="true"; shift 1 ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown schedule arg: $1" ;;
    esac
  done

  validate_key_delay "$key_delay"
  [[ "$cooldown_min" =~ ^[0-9]+$ ]] || die "--cooldown-minutes must be a non-negative integer"
  [[ "$renudge_min" =~ ^[0-9]+$ ]] || die "--renudge-minutes must be a non-negative integer"

  # ---- resolve destination + mode -----------------------------------------
  local mode=""
  if [[ -n "$to" ]]; then
    [[ -z "$session" && -z "$pane" && -z "$target" ]] \
      || die "use --to OR --session/--pane OR --target, not several at once"
    target="$(resolve_target "$to")"
    if [[ "${#keys[@]}" -eq 0 ]]; then
      [[ -n "$message" ]] || die "--to needs --message (or one or more --key)"
      keys=("$message" "Enter")
      message=""
    fi
    mode="tmux_keys"
  elif [[ -n "$message" ]]; then
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
    die "provide a destination: --to <#|target|name> --message TEXT  (or --message with --session/--pane, or --target with --key)"
  fi

  # ---- resolve when (--at / --in / --minutes; default: now/next tick) ------
  local now due due_utc
  now="$(date -u +%s)"
  if [[ -n "$at_spec" ]]; then
    due="$(date -u -d "$at_spec" +%s 2>/dev/null)" \
      || die "could not parse --at '$at_spec' (try '9am', '14:30', 'tomorrow 09:00')"
    if (( due < now )); then due=$((due + 86400)); fi
  elif [[ -n "$in_spec" ]]; then
    [[ "$in_spec" =~ ^([0-9]+)([smhd])$ ]] || die "--in must look like 90s / 30m / 5h / 2d"
    local n="${BASH_REMATCH[1]}" unit="${BASH_REMATCH[2]}" secs=0
    case "$unit" in
      s) secs=$((n)) ;; m) secs=$((n * 60)) ;; h) secs=$((n * 3600)) ;; d) secs=$((n * 86400)) ;;
    esac
    due=$((now + secs))
  elif [[ -n "$minutes" ]]; then
    [[ "$minutes" =~ ^[0-9]+$ ]] || die "--minutes must be a non-negative integer"
    due=$((now + minutes * 60))
  else
    due=$now
  fi
  due_utc="$(date -u -d "@$due" +%Y-%m-%dT%H:%M:%SZ)"

  # ---- preflight: destination must be live now (unless --force) ------------
  if [[ "$force" != "true" ]]; then
    if [[ "$mode" == "tmux_keys" && -n "$target" ]] && ! target_exists "$target"; then
      die "target '$target' is not a live tmux pane right now.
  see targets:     sbp send-later panes
  schedule anyway: re-run with --force"
    elif [[ "$mode" == "ntm_send" ]] && ! tmux has-session -t "=$session" 2>/dev/null; then
      die "session '$session' is not a live tmux session right now.
  see targets:     sbp send-later panes
  schedule anyway: re-run with --force"
    fi
  fi

  # ---- auto-generate id when not supplied ----------------------------------
  if [[ -z "$id" ]]; then
    local base
    if [[ "$mode" == "tmux_keys" ]]; then base="$target"; else base="${session}-${pane}"; fi
    base="$(printf 'sl-%s-%s' "$base" "$(date -u +%H%M%S)" | tr -c 'A-Za-z0-9_.-' '-')"
    id="$(sanitize_id "$base")"
  fi

  mkdir -p "$STATE_DIR" "$LOG_DIR"
  install_cron >/dev/null

  local job keys_joined
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

  local dest
  if [[ "$mode" == "tmux_keys" ]]; then dest="$target"; else dest="session $session pane $pane"; fi
  echo "scheduled: id=$id -> $dest"
  echo "  when=$due_utc  mode=$mode  recurring=$recurring  when_waiting=$when_waiting  replaced=$replaced"
  echo "  state: $job"
  echo "next: sbp send-later list   |   cancel: sbp send-later cancel $id"
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
  # A missing/dead target pane must not abort the batch: under `set -e` +
  # `pipefail` a failing tmux capture would otherwise propagate out of the
  # command substitution and kill run-pending. Swallow it -> empty capture.
  tmux capture-pane -p -t "$target" -S "-${CAPTURE_LINES}" 2>/dev/null \
    | sed -e 's/█//g' -e 's/[[:space:]]*$//' || true
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
  # Default DUE_EPOCH so jobs written before this field existed don't trip set -u.
  local due_epoch="${DUE_EPOCH:-0}"
  [[ "$now" -ge "$due_epoch" ]] || return 0

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
  local job rc
  shopt -s nullglob
  for job in "$STATE_DIR"/*.env; do
    # Isolate each job in a subshell so ANY failure in one job (vanished gate
    # target, malformed env, set -e/-u abort) can never wedge the whole batch.
    rc=0
    ( run_job "$job" ) || rc=$?
    if [[ "$rc" -ne 0 ]]; then
      echo "$(date -u +%FT%TZ) run_job failed job=$job rc=$rc" >> "$LOG_DIR/ntm-send-later.cron.log"
    fi
  done
}

list_jobs() {
  mkdir -p "$STATE_DIR" "$LOG_DIR"
  local job id due mode status
  shopt -s nullglob
  local jobs=("$STATE_DIR"/*.env)
  echo "jobs: ${#jobs[@]}"
  if [[ "${#jobs[@]}" -eq 0 ]]; then
    echo "next: sbp send-later new   (interactive)   |   sbp send-later panes   (list targets)"
    return 0
  fi
  for job in "${jobs[@]}"; do
    # shellcheck disable=SC1090
    source "$job"
    id="${ID:-$(basename "$job" .env)}"
    due="${DUE_UTC:-?}"
    mode="${MODE:-?}"
    status="pending"
    [[ "${RECURRING:-false}" == "true" ]] && status="recurring"
    [[ -f "$STATE_DIR/$id.done" ]] && status="done"
    echo "- id=$id status=$status due_utc=$due mode=$mode recurring=${RECURRING:-false} gate=${WHEN_WAITING:-false} job=$job"
  done
  echo "next: sbp send-later new | panes | cancel ID | run-pending"
}

cancel_job() {
  local id="${1:-}"
  [[ -n "$id" ]] || die "usage: sbp send-later cancel ID"
  id="$(sanitize_id "$id")"
  local removed=0 f
  for f in env done last sentfp lastsent lock; do
    if [[ -e "$STATE_DIR/$id.$f" ]]; then
      rm -f "$STATE_DIR/$id.$f"
      removed=1
    fi
  done
  if [[ "$removed" == "1" ]]; then
    echo "cancelled: id=$id (job + state removed; cron wrapper left in place)"
  else
    echo "no such job: id=$id"
  fi
}

main() {
  local cmd="${1:-}"
  shift || true
  case "$cmd" in
    install-cron) install_cron ;;
    schedule) schedule_job "$@" ;;
    run-pending) run_pending ;;
    list) list_jobs ;;
    panes|targets) cmd_panes "$@" ;;
    new|wizard) wizard_new ;;
    cancel|remove) cancel_job "$@" ;;
    -h|--help) usage ;;
    "") list_jobs ;;
    *) die "unknown command: $cmd" ;;
  esac
}

main "$@"
