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
CRON_LOG="${LOG_DIR}/ntm-send-later.cron.log"

# --- launch mode -------------------------------------------------------------
# The `launch` MODE runs an operator-controlled *launcher* by name (e.g. a
# self-scheduling burndown tick) instead of typing into a pane. It is NOT a
# general command executor: a job may only name a launcher that (a) matches a
# traversal-safe slug, (b) lives directly in LAUNCHER_DIR, (c) is a regular
# executable file owned by this user, and it is always run with NO arguments.
# This keeps `.env` jobs declarative and auditable while letting send-later own
# the recurring scheduler, cron heartbeat, per-job flock, and list/doctor/gc.
LAUNCHER_DIR="${SBP_SEND_LATER_LAUNCHER_DIR:-$HOME/.local/share/sbp/launchers}"

run_launcher() {  # $1 = launcher slug; echoes nothing, returns launcher's rc
  local name="$1" path
  [[ "$name" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "invalid launcher slug: $name" >&2; return 2; }
  path="$LAUNCHER_DIR/$name"
  [[ -f "$path" && -x "$path" ]] || { echo "launcher not found/executable: $path" >&2; return 2; }
  [[ -O "$path" ]] || { echo "launcher not owned by current user: $path" >&2; return 2; }
  "$path"
}
# Heartbeat written every tick so `doctor` can prove the per-minute run-pending
# is firing even when the queue is empty (an idle run-pending writes nothing
# else, so CRON_LOG mtime alone cannot distinguish "dead" from "idle").
TICK_FILE="${STATE_DIR}/.last-tick"

# A one-shot job past its due time by more than this with no .done is "overdue"
# (suspicious — it should have fired). Below this it is merely "due".
OVERDUE_GRACE_S="${SBP_SEND_LATER_OVERDUE_GRACE_S:-120}"
# The per-minute tick should touch CRON_LOG often; older than this => "stale".
TICK_STALE_S="${SBP_SEND_LATER_TICK_STALE_S:-180}"

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
  sbp send-later list [--json]         List scheduled jobs (+ health fields)
  sbp send-later doctor [--json]       Health check: cron tick + wedged/overdue jobs
  sbp send-later cancel ID             Remove a scheduled job
  sbp send-later cancel --all          Remove every scheduled job
  sbp send-later cancel --match GLOB   Remove jobs whose id matches a glob
  sbp send-later cancel --done         Remove only completed jobs
  sbp send-later fire ID               Fire one job NOW (ignores due time; for testing)
  sbp send-later gc                    Sweep orphaned logs/state (no live .env)
  sbp send-later install-cron          Install the per-minute run-pending tick
  sbp send-later run-pending           Fire any due jobs (called by cron)

  sbp send-later schedule --to <#|target|name> --message TEXT [--in 5h|--at 9am] [gate flags]
  sbp send-later schedule --target SESS:WIN.PANE --key KEY [--key KEY ...] [--key-delay S] [when] [gate flags]
  sbp send-later schedule --session S --pane P --message TEXT [when] [gate flags]

When (default = now / next tick):
  --in DURATION          90s | 30m | 5h | 2d
  --at TIME              "9am" | "14:30" | "tomorrow 09:00"   (parsed in local time)
  --minutes N            fire N minutes from now
  --tz ZONE              interpret --at / --until in this timezone (e.g. America/New_York)

Targeting:
  --to <#|target|name>   pick by panes-list number, session:win.pane, or fuzzy agent/title
  --force                schedule even if the target pane is not live yet
  --dry-run              resolve + preview the job; do NOT schedule it
  --id ID                job id (auto-generated from target + time if omitted)

Gate / recurring flags (optional):
  --recurring            Keep re-evaluating every cron tick instead of firing once.
  --when-waiting         Only fire when the target pane looks idle/waiting (stable
                         tail + idle footer + no busy indicator), never mid-run.
  --max-fires N          Stop (mark done) after N successful sends (0 = unlimited).
  --until TIME           Stop (mark done) at this wall-clock time or duration (5h).
  --cooldown-minutes N   Minimum minutes between sends (default 0).
  --renudge-minutes N    Re-send the SAME idle screen after N minutes (default 0 = never).
  --idle-regex RE        Override the "waiting" footer signature (extended regex).
  --busy-regex RE        Override the "running" signature (extended regex).

Examples:
  # Easiest: pick a pane and answer a few prompts
  sbp send-later new

  # One-shot, fire in 5h into pane #3 from `sbp send-later panes`:
  sbp send-later schedule --to 3 --in 5h --message "continue"

  # Auto-continue: nudge a pane ONLY when it goes idle, up to 12 times:
  sbp send-later schedule --to grok --message continue --recurring --when-waiting --max-fires 12

  # Preview without scheduling, then fire once to test the send path:
  sbp send-later schedule --to 3 --in 5h --message hi --dry-run

  # Raw keys (advanced) at a wall-clock time:
  sbp send-later schedule --target devbox-1:0.0 --key 1 --key Enter --key continue --key Enter --at "tomorrow 09:00"
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 2
}

need_jq() {
  command -v jq >/dev/null 2>&1 || die "--json output needs jq on PATH"
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

# Format an epoch as UTC ISO-8601 / local wall clock. Used everywhere we show a
# time so humans see local AND the canonical UTC the job stores.
fmt_utc() {
  [[ "${1:-0}" -gt 0 ]] || { printf '?'; return 0; }
  date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf '?'
}
fmt_local() {
  [[ "${1:-0}" -gt 0 ]] || { printf 'never'; return 0; }
  date -d "@$1" +'%Y-%m-%d %H:%M %Z' 2>/dev/null || printf '?'
}

# Resolve a wall-clock time expression to an epoch, optionally in a given TZ.
# Default (no tz) parses in the box's local time, NOT UTC -- "9am" means 9am
# locally, which is what humans mean. The stored DUE_UTC is still canonical.
resolve_at_epoch() {
  local spec="$1" tz="${2:-}" e
  if [[ -n "$tz" ]]; then
    e="$(TZ="$tz" date -d "$spec" +%s 2>/dev/null)" || return 1
  else
    e="$(date -d "$spec" +%s 2>/dev/null)" || return 1
  fi
  [[ -n "$e" ]] || return 1
  printf '%s' "$e"
}

# Resolve a deadline (--until): accept a duration (5h / 30m) OR a wall-clock time.
resolve_deadline_epoch() {
  local spec="$1" tz="${2:-}"
  if [[ "$spec" =~ ^([0-9]+)([smhd])$ ]]; then
    local n="${BASH_REMATCH[1]}" unit="${BASH_REMATCH[2]}" secs=0
    case "$unit" in
      s) secs=$((n)) ;; m) secs=$((n * 60)) ;; h) secs=$((n * 3600)) ;; d) secs=$((n * 86400)) ;;
    esac
    printf '%s' "$(( $(date -u +%s) + secs ))"
    return 0
  fi
  resolve_at_epoch "$spec" "$tz"
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

cron_installed() {
  crontab -l 2>/dev/null | grep -q "$CRON_MARKER"
}

have_tty() {
  [[ -t 0 && -t 1 ]]
}

# Emit one TSV row per live tmux pane: idx, target, agent, state, title, here.
# Default is instant (tmux only): AGENT is the pane's current command, STATE blank.
# Pass "rich" as $1 to enrich AGENT/STATE from `ntm --robot-snapshot` -- that call
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
    need_jq
    if [[ -z "$rows" ]]; then echo "[]"; return 0; fi
    printf '%s\n' "$rows" | jq -R -s '
      split("\n") | map(select(length > 0)) | map(split(""))
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
  local idle_re="" busy_re="" force="false" dry_run="false" tz="" max_fires="0" until_spec=""
  local launcher=""
  local keys=()

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --minutes) require_value "$1" "$#"; minutes="$2"; shift 2 ;;
      --in) require_value "$1" "$#"; in_spec="$2"; shift 2 ;;
      --at) require_value "$1" "$#"; at_spec="$2"; shift 2 ;;
      --tz) require_value "$1" "$#"; tz="$2"; shift 2 ;;
      --id) require_value "$1" "$#"; id="$(sanitize_id "$2")"; shift 2 ;;
      --session) require_value "$1" "$#"; session="$2"; shift 2 ;;
      --pane) require_value "$1" "$#"; pane="$2"; shift 2 ;;
      --message) require_value "$1" "$#"; message="$2"; shift 2 ;;
      --target) require_value "$1" "$#"; target="$2"; shift 2 ;;
      --to) require_value "$1" "$#"; to="$2"; shift 2 ;;
      --key) require_value "$1" "$#"; keys+=("$2"); shift 2 ;;
      --key-delay) require_value "$1" "$#"; key_delay="$2"; shift 2 ;;
      --launcher) require_value "$1" "$#"; launcher="$2"; shift 2 ;;
      --recurring) recurring="true"; shift 1 ;;
      --when-waiting) when_waiting="true"; shift 1 ;;
      --max-fires) require_value "$1" "$#"; max_fires="$2"; shift 2 ;;
      --until|--expire-at) require_value "$1" "$#"; until_spec="$2"; shift 2 ;;
      --cooldown-minutes) require_value "$1" "$#"; cooldown_min="$2"; shift 2 ;;
      --renudge-minutes) require_value "$1" "$#"; renudge_min="$2"; shift 2 ;;
      --idle-regex) require_value "$1" "$#"; idle_re="$2"; shift 2 ;;
      --busy-regex) require_value "$1" "$#"; busy_re="$2"; shift 2 ;;
      --force) force="true"; shift 1 ;;
      --dry-run) dry_run="true"; shift 1 ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown schedule arg: $1" ;;
    esac
  done

  validate_key_delay "$key_delay"
  [[ "$cooldown_min" =~ ^[0-9]+$ ]] || die "--cooldown-minutes must be a non-negative integer"
  [[ "$renudge_min" =~ ^[0-9]+$ ]] || die "--renudge-minutes must be a non-negative integer"
  [[ "$max_fires" =~ ^[0-9]+$ ]] || die "--max-fires must be a non-negative integer"

  # ---- resolve destination + mode -----------------------------------------
  local mode=""
  if [[ -n "$launcher" ]]; then
    [[ -z "$to$session$pane$target$message" && "${#keys[@]}" -eq 0 ]] \
      || die "--launcher takes no pane destination (no --to/--session/--pane/--target/--message/--key)"
    [[ "$launcher" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "--launcher must be a slug ^[a-z0-9][a-z0-9-]*$"
    [[ -x "$LAUNCHER_DIR/$launcher" ]] \
      || die "launcher '$launcher' not found or not executable in $LAUNCHER_DIR
  register it first (chmod +x + place/symlink in $LAUNCHER_DIR), then re-run"
    mode="launch"
  elif [[ -n "$to" ]]; then
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
    due="$(resolve_at_epoch "$at_spec" "$tz")" \
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

  # ---- resolve --until (deadline for recurring/long jobs) ------------------
  local expire_epoch="0" expire_utc=""
  if [[ -n "$until_spec" ]]; then
    expire_epoch="$(resolve_deadline_epoch "$until_spec" "$tz")" \
      || die "could not parse --until '$until_spec' (try '5h', '2026-06-25 09:00')"
    (( expire_epoch > now )) || die "--until '$until_spec' is in the past"
    (( expire_epoch >= due )) || die "--until is before the first fire time"
    expire_utc="$(date -u -d "@$expire_epoch" +%Y-%m-%dT%H:%M:%SZ)"
  fi

  # ---- auto-generate id when not supplied ----------------------------------
  if [[ -z "$id" ]]; then
    local base
    if [[ "$mode" == "launch" ]]; then base="launch-$launcher";
    elif [[ "$mode" == "tmux_keys" ]]; then base="$target"; else base="${session}-${pane}"; fi
    base="$(printf 'sl-%s-%s' "$base" "$(date -u +%H%M%S)" | tr -c 'A-Za-z0-9_.-' '-')"
    id="$(sanitize_id "$base")"
  fi

  local keys_joined=""
  if [[ "${#keys[@]}" -gt 0 ]]; then
    keys_joined="$(printf '%s\n' "${keys[@]}")"
  fi

  local dest
  if [[ "$mode" == "launch" ]]; then dest="launcher:$launcher";
  elif [[ "$mode" == "tmux_keys" ]]; then dest="$target"; else dest="session $session pane $pane"; fi

  # ---- dry-run: show the fully-resolved job, write nothing -----------------
  if [[ "$dry_run" == "true" ]]; then
    echo "DRY RUN — nothing scheduled."
    echo "  id=$id  ->  $dest"
    echo "  when=$due_utc  ($(fmt_local "$due") local)  mode=$mode"
    if [[ "$mode" == "launch" ]]; then
      echo "  launcher: $LAUNCHER_DIR/$launcher"
    elif [[ "$mode" == "tmux_keys" ]]; then
      echo "  keys: $(printf '%s' "$keys_joined" | tr '\n' '|' | sed 's/|$//')"
    else
      echo "  message: $message"
    fi
    echo "  recurring=$recurring  when_waiting=$when_waiting  max_fires=$max_fires  until=${expire_utc:-none}"
    if [[ "$mode" == "tmux_keys" && -n "$target" ]] && ! target_exists "$target"; then
      echo "  WARN: target '$target' is not live right now (would need --force to schedule)"
    elif [[ "$mode" == "ntm_send" ]] && ! tmux has-session -t "=$session" 2>/dev/null; then
      echo "  WARN: session '$session' is not live right now (would need --force to schedule)"
    fi
    echo "schedule for real: drop --dry-run"
    return 0
  fi

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

  mkdir -p "$STATE_DIR" "$LOG_DIR"
  install_cron >/dev/null

  local job
  job="$STATE_DIR/$id.env"
  local replaced="false"
  if [[ -f "$job" ]]; then
    replaced="true"
  fi
  rm -f "$STATE_DIR/$id.done" "$STATE_DIR/$id.last" "$STATE_DIR/$id.sentfp" \
        "$STATE_DIR/$id.lastsent" "$STATE_DIR/$id.fires"

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
MAX_FIRES='$max_fires'
EXPIRE_EPOCH='$expire_epoch'
EXPIRE_UTC='$expire_utc'
COOLDOWN_MIN='$cooldown_min'
RENUDGE_MIN='$renudge_min'
IDLE_RE_B64='$(b64 "$idle_re")'
BUSY_RE_B64='$(b64 "$busy_re")'
LAUNCHER_B64='$(b64 "$launcher")'
EOF

  echo "scheduled: id=$id -> $dest"
  echo "  when=$due_utc  ($(fmt_local "$due") local)  mode=$mode  recurring=$recurring  when_waiting=$when_waiting  replaced=$replaced"
  [[ "$max_fires" -gt 0 ]] && echo "  max_fires=$max_fires"
  [[ -n "$expire_utc" ]] && echo "  until=$expire_utc  ($(fmt_local "$expire_epoch") local)"
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
# For ntm_send mode we must find the window that actually CONTAINS pane index P
# (pane indices repeat across windows), not blindly assume window 0.
gate_target() {
  if [[ "$MODE" == "tmux_keys" ]]; then
    unb64 "$TARGET_B64"
    return 0
  fi
  local s p win=""
  s="$(unb64 "$SESSION_B64")"
  p="$(unb64 "$PANE_B64")"
  # Scan every pane in the session; prefer the active pane on a tie.
  win="$(tmux list-panes -s -t "=$s" -F '#{pane_index} #{window_index} #{pane_active}' 2>/dev/null \
    | awk -v p="$p" '$1==p { if ($3==1) { print $2; exit } if (first=="") first=$2 } END { if (first!="") print first }' \
    | head -n1)"
  [[ -n "$win" ]] || win="$(tmux list-windows -t "=$s:" -F '#{window_index}' 2>/dev/null | head -n1)"
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

# Write a .done marker (stops one-shot jobs AND bounded recurring jobs).
mark_done() {
  local job="$1" done_file="$2" reason="${3:-completed}"
  {
    echo "completed_at=$(date -u +%FT%TZ)"
    echo "reason=$reason"
    echo "job=$job"
  } > "$done_file"
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
  local max_fires="${MAX_FIRES:-0}"
  local expire_epoch="${EXPIRE_EPOCH:-0}"
  local idle_re busy_re
  idle_re="$(unb64 "${IDLE_RE_B64:-}")"; [[ -n "$idle_re" ]] || idle_re="$DEFAULT_IDLE_RE"
  busy_re="$(unb64 "${BUSY_RE_B64:-}")"; [[ -n "$busy_re" ]] || busy_re="$DEFAULT_BUSY_RE"

  # `fire ID` sets these to manually trigger a single job for testing.
  local force_fire="${SL_FIRE_FORCE:-0}"
  local ignore_gate="${SL_FIRE_IGNORE_GATE:-0}"

  local now done_file log_file lock_file last_file fp_file sent_file fires_file
  now="$(date -u +%s)"
  # Default DUE_EPOCH so jobs written before this field existed don't trip set -u.
  local due_epoch="${DUE_EPOCH:-0}"
  if [[ "$force_fire" != "1" ]]; then
    [[ "$now" -ge "$due_epoch" ]] || return 0
  fi

  done_file="$STATE_DIR/$ID.done"
  log_file="$STATE_DIR/$ID.log"
  lock_file="$STATE_DIR/$ID.lock"
  last_file="$STATE_DIR/$ID.last"
  fp_file="$STATE_DIR/$ID.sentfp"
  sent_file="$STATE_DIR/$ID.lastsent"
  fires_file="$STATE_DIR/$ID.fires"

  # Once a job is done (one-shot fired, or bounded job hit its limit/deadline)
  # it stops -- unless explicitly re-fired.
  if [[ "$force_fire" != "1" && -f "$done_file" ]]; then
    return 0
  fi

  exec 9>"$lock_file"
  if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) id=$ID already_running" >> "$log_file"
    return 0
  fi

  # Deadline reached: stop the job for good.
  if [[ "$expire_epoch" -gt 0 && "$now" -ge "$expire_epoch" ]]; then
    echo "$(date -u +%FT%TZ) id=$ID expired (until=${EXPIRE_UTC:-?}) -> done" >> "$log_file"
    mark_done "$job" "$done_file" expired
    return 0
  fi

  local fires=0
  [[ -f "$fires_file" ]] && fires="$(cat "$fires_file" 2>/dev/null || echo 0)"
  # Fire budget already spent: stop for good.
  if [[ "$max_fires" -gt 0 && "$fires" -ge "$max_fires" ]]; then
    echo "$(date -u +%FT%TZ) id=$ID max_fires=$max_fires reached -> done" >> "$log_file"
    mark_done "$job" "$done_file" max_fires
    return 0
  fi

  local fp=""
  if [[ "$when_waiting" == "true" && "$ignore_gate" != "1" ]]; then
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
    local target keys_text key rc
    target="$(unb64 "$TARGET_B64")"
    keys_text="$(unb64 "$KEYS_B64")"
    # A failed send-keys (e.g. the pane vanished after the gate check) must
    # propagate -- otherwise the trailing `sleep` would mask it as success and a
    # one-shot would be marked done having sent nothing. Capture the real rc
    # directly: `if ! cmd; then status=$?` would record the negation's 0, not
    # the failure.
    while IFS= read -r key; do
      [[ -n "$key" ]] || continue
      send_tmux_key "$target" "$key" >> "$log_file" 2>&1
      rc=$?
      if [[ "$rc" -ne 0 ]]; then
        status=$rc
        echo "$(date -u +%FT%TZ) id=$ID send-keys failed key=$key target=$target rc=$rc" >> "$log_file"
        break
      fi
      sleep "$KEY_DELAY"
    done <<< "$keys_text"
  elif [[ "$MODE" == "launch" ]]; then
    run_launcher "$(unb64 "${LAUNCHER_B64:-}")" >> "$log_file" 2>&1
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
    fires=$((fires + 1))
    printf '%s' "$fires" > "$fires_file"
    local reached_max="false"
    [[ "$max_fires" -gt 0 && "$fires" -ge "$max_fires" ]] && reached_max="true"
    if [[ "$recurring" != "true" ]]; then
      mark_done "$job" "$done_file" completed
    elif [[ "$reached_max" == "true" ]]; then
      echo "$(date -u +%FT%TZ) id=$ID fired $fires/$max_fires -> done" >> "$log_file"
      mark_done "$job" "$done_file" max_fires
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
      echo "$(date -u +%FT%TZ) run_job failed job=$job rc=$rc" >> "$CRON_LOG"
    fi
  done
  # Heartbeat last: a stale TICK_FILE => the tick itself stopped firing.
  date -u +%s > "$TICK_FILE" 2>/dev/null || true
}

# Manually fire a single job NOW, ignoring its due time and .done marker. For
# testing targeting/gating without waiting for the next cron minute.
cmd_fire() {
  local id="" ignore_gate="0"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --ignore-gate) ignore_gate="1" ;;
      -*) die "unknown fire arg: $1" ;;
      *) [[ -z "$id" ]] || die "fire takes one ID"; id="$1" ;;
    esac
    shift
  done
  [[ -n "$id" ]] || die "usage: sbp send-later fire ID [--ignore-gate]"
  id="$(sanitize_id "$id")"
  local job="$STATE_DIR/$id.env"
  [[ -f "$job" ]] || die "no such job: $id (run: sbp send-later list)"
  echo "firing now: id=$id"
  SL_FIRE_FORCE=1 SL_FIRE_IGNORE_GATE="$ignore_gate" run_job "$job"
  echo "done. log tail:"
  tail -n 3 "$STATE_DIR/$id.log" 2>/dev/null || echo "  (no log)"
}

# Emit one \x1f-delimited record per job, with computed health fields. Sourced
# in a subshell so stale fields never leak between jobs. Shared by list+doctor.
# Fields: id mode recurring gate due_utc due_epoch status last_fire_epoch
#         last_status overdue_s fires max_fires expire_utc dest
job_record() {
  local job="$1"
  (
    set +u
    # shellcheck disable=SC1090
    source "$job"
    local now id mode recurring gate due_utc due_epoch dest
    now="$(date -u +%s)"
    id="${ID:-$(basename "$job" .env)}"
    mode="${MODE:-?}"
    recurring="${RECURRING:-false}"
    gate="${WHEN_WAITING:-false}"
    due_utc="${DUE_UTC:-?}"
    due_epoch="${DUE_EPOCH:-0}"
    if [[ "$mode" == "tmux_keys" ]]; then
      dest="$(unb64 "${TARGET_B64:-}" 2>/dev/null || true)"
    else
      dest="$(unb64 "${SESSION_B64:-}" 2>/dev/null || true):$(unb64 "${PANE_B64:-}" 2>/dev/null || true)"
    fi

    local done_file="$STATE_DIR/$id.done"
    local sent_file="$STATE_DIR/$id.lastsent"
    local log_file="$STATE_DIR/$id.log"
    local fires_file="$STATE_DIR/$id.fires"

    local last_fire_epoch=0
    [[ -f "$sent_file" ]] && last_fire_epoch="$(cat "$sent_file" 2>/dev/null || echo 0)"
    local last_status="-"
    if [[ -f "$log_file" ]]; then
      last_status="$(grep -oE 'status=[0-9]+' "$log_file" 2>/dev/null | tail -n1 | cut -d= -f2)"
      [[ -n "$last_status" ]] || last_status="-"
    fi
    local fires=0
    [[ -f "$fires_file" ]] && fires="$(cat "$fires_file" 2>/dev/null || echo 0)"

    local overdue_s=0 status
    if [[ -f "$done_file" ]]; then
      status="done"
      local reason; reason="$(grep -oE '^reason=.*' "$done_file" 2>/dev/null | cut -d= -f2)"
      [[ -n "$reason" && "$reason" != "completed" ]] && status="done:$reason"
    elif (( now < due_epoch )); then
      status="pending"
    elif [[ "$recurring" == "true" ]]; then
      status="recurring"
    else
      overdue_s=$(( now - due_epoch ))
      if (( overdue_s > OVERDUE_GRACE_S )); then status="overdue"; else status="due"; fi
    fi

    printf '%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\n' \
      "$id" "$mode" "$recurring" "$gate" "$due_utc" "$due_epoch" "$status" \
      "$last_fire_epoch" "$last_status" "$overdue_s" "$fires" "${MAX_FIRES:-0}" \
      "${EXPIRE_UTC:-}" "$dest"
  )
}

all_job_records() {
  shopt -s nullglob
  local job
  for job in "$STATE_DIR"/*.env; do
    job_record "$job"
  done
}

list_jobs() {
  local json="false"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) json="true" ;;
      --format) [[ "${2:-}" == "json" ]] || die "list: --format only supports json"; json="true"; shift ;;
      *) die "unknown list arg: $1 (use --json)" ;;
    esac
    shift
  done
  mkdir -p "$STATE_DIR" "$LOG_DIR"
  local records; records="$(all_job_records)"

  if [[ "$json" == "true" ]]; then
    need_jq
    if [[ -z "$records" ]]; then echo '{"jobs":[],"count":0}'; return 0; fi
    printf '%s\n' "$records" | jq -R -s '
      split("\n") | map(select(length > 0)) | map(split(""))
      | map({
          id:.[0], mode:.[1], recurring:(.[2]=="true"), gate:(.[3]=="true"),
          due_utc:.[4], due_epoch:(.[5]|tonumber),
          status:.[6],
          last_fire_utc:(if (.[7]|tonumber) > 0 then (.[7]|tonumber|todate) else null end),
          last_status:(if .[8]=="-" then null else .[8] end),
          overdue_s:(.[9]|tonumber), fires:(.[10]|tonumber),
          max_fires:(.[11]|tonumber),
          until_utc:(if .[12]=="" then null else .[12] end),
          dest:.[13]
        })
      | {jobs:., count:length}'
    return 0
  fi

  local count=0
  [[ -n "$records" ]] && count="$(printf '%s\n' "$records" | grep -c . || true)"
  echo "jobs: $count"
  if [[ "$count" -eq 0 ]]; then
    echo "next: sbp send-later new   (interactive)   |   sbp send-later panes   (list targets)"
    return 0
  fi
  printf '  %-26s %-9s %-10s %-20s %s\n' "ID" "STATUS" "MODE" "DUE (local)" "DEST"
  local id mode recurring gate due_utc due_epoch status _lfe lstat ovd fires _maxf _exp dest
  while IFS=$'\x1f' read -r id mode recurring gate due_utc due_epoch status _lfe lstat ovd fires _maxf _exp dest; do
    [[ -n "$id" ]] || continue
    printf '  %-26s %-9s %-10s %-20s %s\n' "$id" "$status" "$mode" "$(fmt_local "$due_epoch")" "$dest"
  done <<< "$records"
  echo "next: sbp send-later doctor | new | panes | cancel ID | fire ID | run-pending"
}

# Health check: is the cron tick alive, and are any jobs wedged/overdue?
cmd_doctor() {
  local json="false"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) json="true" ;;
      --format) [[ "${2:-}" == "json" ]] || die "doctor: --format only supports json"; json="true"; shift ;;
      *) die "unknown doctor arg: $1 (use --json)" ;;
    esac
    shift
  done
  mkdir -p "$STATE_DIR" "$LOG_DIR"
  local now; now="$(date -u +%s)"

  # --- cron tick health ---
  # Prefer the per-tick heartbeat (proves the tick fires even with an empty
  # queue); fall back to CRON_LOG mtime for installs predating the heartbeat.
  local cron_ok="false"; cron_installed && cron_ok="true"
  local tick_epoch=0 tick_age=-1 tick_stale="false"
  if [[ -f "$TICK_FILE" ]]; then
    tick_epoch="$(cat "$TICK_FILE" 2>/dev/null || echo 0)"
  elif [[ -f "$CRON_LOG" ]]; then
    tick_epoch="$(stat -c %Y "$CRON_LOG" 2>/dev/null || echo 0)"
  fi
  [[ "$tick_epoch" =~ ^[0-9]+$ ]] || tick_epoch=0
  if (( tick_epoch > 0 )); then
    tick_age=$(( now - tick_epoch ))
    (( tick_age > TICK_STALE_S )) && tick_stale="true"
  fi

  # --- per-job health ---
  local records; records="$(all_job_records)"
  local total=0 overdue=0 wedged=0 active=0
  local issues=()
  local id mode recurring gate due_utc due_epoch status _lfe lstat ovd fires _maxf _exp dest
  while IFS=$'\x1f' read -r id mode recurring gate due_utc due_epoch status _lfe lstat ovd fires _maxf _exp dest; do
    [[ -n "$id" ]] || continue
    total=$((total + 1))
    case "$status" in
      done:*|done) ;;
      *) active=$((active + 1)) ;;
    esac
    if [[ "$status" == "overdue" ]]; then
      overdue=$((overdue + 1))
      issues+=("overdue: $id due $due_utc, ${ovd}s ago, last_status=${lstat}")
    fi
    # Wedged: an active job exists but the tick that should fire it is stale.
    if [[ "$tick_stale" == "true" && "$status" != "done" && "$status" != done:* && "$status" != "pending" ]]; then
      wedged=$((wedged + 1))
    fi
  done <<< "$records"

  # Stale lock files (a crashed run_job can leave one behind).
  shopt -s nullglob
  local stale_locks=0 lf
  for lf in "$STATE_DIR"/*.lock; do
    local lage; lage=$(( now - $(stat -c %Y "$lf" 2>/dev/null || echo "$now") ))
    (( lage > 300 )) && stale_locks=$((stale_locks + 1))
  done

  # Orphaned state (logs/markers with no live .env) -> suggests `gc`.
  local orphans; orphans="$(count_orphans)"

  if [[ "$cron_ok" != "true" ]]; then issues+=("cron tick NOT installed -- run: sbp send-later install-cron"); fi
  if [[ "$tick_stale" == "true" && "$active" -gt 0 ]]; then
    issues+=("cron tick stale (${tick_age}s since last run-pending) with $active active job(s) -- tick may be dead/wedged")
  fi
  (( stale_locks > 0 )) && issues+=("$stale_locks stale lock file(s) (>5m) -- a run_job may have crashed")
  (( orphans > 0 )) && issues+=("$orphans orphaned state/log file(s) -- run: sbp send-later gc")

  local ok="true"; [[ "${#issues[@]}" -gt 0 ]] && ok="false"

  if [[ "$json" == "true" ]]; then
    need_jq
    local issues_json="[]"
    if [[ "${#issues[@]}" -gt 0 ]]; then
      issues_json="$(printf '%s\n' "${issues[@]}" | jq -R -s 'split("\n") | map(select(length>0))')"
    fi
    jq -n \
      --argjson ok "$ok" \
      --argjson cron "$cron_ok" \
      --argjson tick_age "$tick_age" \
      --argjson tick_stale "$tick_stale" \
      --arg last_tick "$(fmt_utc "$tick_epoch")" \
      --argjson total "$total" --argjson active "$active" \
      --argjson overdue "$overdue" --argjson wedged "$wedged" \
      --argjson stale_locks "$stale_locks" --argjson orphans "$orphans" \
      --argjson issues "$issues_json" \
      '{ok:$ok, cron_installed:$cron, last_tick_utc:$last_tick, tick_age_s:$tick_age,
        tick_stale:$tick_stale, jobs:{total:$total, active:$active, overdue:$overdue, wedged:$wedged},
        stale_locks:$stale_locks, orphans:$orphans, issues:$issues}'
    return 0
  fi

  if [[ "$ok" == "true" ]]; then
    echo "OK — send-later is healthy"
  else
    echo "ISSUES FOUND:"
    local i; for i in "${issues[@]}"; do echo "  - $i"; done
  fi
  echo
  echo "cron tick:   installed=$cron_ok  last_run=$(fmt_local "$tick_epoch")  age=${tick_age}s  stale=$tick_stale"
  echo "jobs:        total=$total  active=$active  overdue=$overdue  wedged=$wedged"
  echo "hygiene:     stale_locks=$stale_locks  orphans=$orphans"
}

# Count state/log files whose backing .env no longer exists.
count_orphans() {
  shopt -s nullglob
  local f base n=0
  for f in "$STATE_DIR"/*.log "$STATE_DIR"/*.done "$STATE_DIR"/*.last \
           "$STATE_DIR"/*.sentfp "$STATE_DIR"/*.lastsent "$STATE_DIR"/*.lock \
           "$STATE_DIR"/*.fires; do
    base="${f%.*}"
    [[ -f "$base.env" ]] || n=$((n + 1))
  done
  printf '%s' "$n"
}

# Sweep state/log files whose backing .env no longer exists.
cmd_gc() {
  local dry="false"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run|-n) dry="true" ;;
      *) die "unknown gc arg: $1 (use --dry-run)" ;;
    esac
    shift
  done
  mkdir -p "$STATE_DIR"
  shopt -s nullglob
  local f base removed=0
  for f in "$STATE_DIR"/*.log "$STATE_DIR"/*.done "$STATE_DIR"/*.last \
           "$STATE_DIR"/*.sentfp "$STATE_DIR"/*.lastsent "$STATE_DIR"/*.lock \
           "$STATE_DIR"/*.fires; do
    base="${f%.*}"
    if [[ ! -f "$base.env" ]]; then
      if [[ "$dry" == "true" ]]; then
        echo "would remove: $(basename "$f")"
      else
        rm -f "$f"
      fi
      removed=$((removed + 1))
    fi
  done
  if [[ "$dry" == "true" ]]; then
    echo "gc --dry-run: $removed orphaned file(s) would be removed"
  else
    echo "gc: removed $removed orphaned file(s)"
  fi
}

# Remove all state files for a single job id.
remove_job_files() {
  local id="$1" purge="$2" removed=0 f
  local exts=(env "done" last sentfp lastsent lock fires)
  [[ "$purge" == "true" ]] && exts+=(log)
  for f in "${exts[@]}"; do
    if [[ -e "$STATE_DIR/$id.$f" ]]; then
      rm -f "$STATE_DIR/$id.$f"
      removed=1
    fi
  done
  printf '%s' "$removed"
}

cancel_job() {
  local id="" all="false" match="" only_done="false" purge="false"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --all) all="true" ;;
      --done) only_done="true" ;;
      --purge) purge="true" ;;
      --match) require_value "$1" "$#"; match="$2"; shift ;;
      -*) die "unknown cancel arg: $1" ;;
      *) [[ -z "$id" ]] || die "cancel takes one ID (or use --all / --match GLOB)"; id="$1" ;;
    esac
    shift
  done

  mkdir -p "$STATE_DIR"
  shopt -s nullglob

  # Bulk modes: --all / --match GLOB / --done
  if [[ "$all" == "true" || -n "$match" || "$only_done" == "true" ]]; then
    [[ -z "$id" ]] || die "use an explicit ID OR a bulk flag (--all/--match/--done), not both"
    local jobfile jid n=0
    for jobfile in "$STATE_DIR"/*.env; do
      jid="$(basename "$jobfile" .env)"
      if [[ -n "$match" ]]; then
        # shellcheck disable=SC2053
        [[ "$jid" == $match ]] || continue
      fi
      if [[ "$only_done" == "true" && ! -f "$STATE_DIR/$jid.done" ]]; then
        continue
      fi
      remove_job_files "$jid" "$purge" >/dev/null
      echo "cancelled: $jid"
      n=$((n + 1))
    done
    echo "removed $n job(s)$([[ "$purge" == "true" ]] && echo " (incl. logs)")"
    return 0
  fi

  [[ -n "$id" ]] || die "usage: sbp send-later cancel ID  (or --all | --match GLOB | --done)"
  id="$(sanitize_id "$id")"
  if [[ "$(remove_job_files "$id" "$purge")" == "1" ]]; then
    echo "cancelled: id=$id (job + state removed$([[ "$purge" == "true" ]] && echo " incl. log"); cron wrapper left in place)"
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
    fire) cmd_fire "$@" ;;
    list) list_jobs "$@" ;;
    doctor|health) cmd_doctor "$@" ;;
    gc) cmd_gc "$@" ;;
    panes|targets) cmd_panes "$@" ;;
    new|wizard) wizard_new ;;
    cancel|remove) cancel_job "$@" ;;
    -h|--help) usage ;;
    "") list_jobs ;;
    *) die "unknown command: $cmd" ;;
  esac
}

main "$@"
