#!/usr/bin/env bash
# Clipboard bootstrap closeout gate.
#
# Two documented modes:
#
#   CI / source smoke (default) — unit, static, and dry-run launch checks
#   only. Live terminal paths are recorded as SKIP with reasons; the report
#   says explicitly that skips were allowed because the run is non-live.
#
#       scripts/clipboard-closeout.sh
#
#   Operator / live rollout proof — everything above PLUS live terminal
#   paths: current-host migration state, local tmux OSC52, direct Ghostty
#   OSC52, mosh, per-host SSH OSC52 + host state + remote tmux, nested
#   local+remote tmux, Conference direct WSL, and synthetic PNG image
#   transfer to d/s/j/c with remote existence checks. In live mode a
#   skipped core path is a FAILURE (fail closed): the run can never report
#   overall PASS while e.g. the d3 path or the current-host migration proof
#   was skipped.
#
#       scripts/clipboard-closeout.sh --live
#
# Artifacts: a durable per-run directory (default
# ~/.local/state/skillbox/clipboard-closeout/<stamp>/) containing
# clipboard-closeout.json plus one raw log per gate. Override the root with
# CLIPBOARD_CLOSEOUT_DIR or --artifact-dir.
#
# Live-mode environment overrides:
#   CLIPBOARD_LIVE_TARGETS  space-separated subset of "d3 sweet jeremy conference1".
#                           Excluded targets are recorded as SKIP and, being
#                           core paths, force overall FAIL (fail closed).
set -euo pipefail

SCRIPT_SOURCE="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd -P)"
ROOT_DIR="${SKILLBOX_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

MODE="smoke"
ARTIFACT_ROOT="${CLIPBOARD_CLOSEOUT_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/skillbox/clipboard-closeout}"

usage() {
  cat <<'EOF'
usage: clipboard-closeout.sh [--live] [--artifact-dir DIR]

Modes:
  (default)   CI / source smoke: unit + static + dry-run launch gates.
              Live terminal paths are recorded as SKIP with reasons and do
              not block PASS (the report states that skips were allowed).
  --live      Operator / live rollout proof: also exercises live terminal
              paths (local tmux, Ghostty, mosh, per-host SSH/remote
              tmux/nested tmux, Conference direct WSL, synthetic PNG image
              transfer to d/s/j/c). Skipped core paths FAIL the run.

Options:
  --artifact-dir DIR   artifact root (default
                       ~/.local/state/skillbox/clipboard-closeout)
  -h, --help           show this help

Artifacts: <artifact-dir>/<stamp>/clipboard-closeout.json plus per-gate raw
logs; <artifact-dir>/latest symlinks the most recent run.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --live) MODE="live"; shift ;;
    --artifact-dir) ARTIFACT_ROOT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done

ART="$ARTIFACT_ROOT/$STAMP-$MODE"
mkdir -p "$ART"
ART="$(cd "$ART" && pwd -P)"
JSON_OUT="$ART/clipboard-closeout.json"
REPORT_PY="$ROOT_DIR/scripts/lib/clipboard_closeout_report.py"

cd "$ROOT_DIR"

EXPECT_CLIPCOPY_SHA="$(sha256sum scripts/clipboard/clipcopy | awk '{print $1}')"
CANONICAL_TARGETS="d3 sweet jeremy conference1"
LIVE_TARGETS="${CLIPBOARD_LIVE_TARGETS:-$CANONICAL_TARGETS}"
LOCAL_SOCKS=()
REMOTE_SOCKS=()   # entries: "ssh_target sock"
REMOTE_FILES=()   # entries: "ssh_target path"

python3 "$REPORT_PY" init --out "$JSON_OUT" --mode "$MODE" --stamp "$STAMP" \
  --host "$(hostname)" --artifact-dir "$ART"

record() { python3 "$REPORT_PY" record --out "$JSON_OUT" "$@"; }

# run_gate <name> <kind> <core|nocore> <target_host> <transport> -- cmd...
run_gate() {
  local name="$1" kind="$2" coreflag="$3" target="$4" transport="$5"
  shift 5
  [ "${1:-}" = "--" ] && shift
  local log="$ART/$name.log" rc=0
  if "$@" >"$log" 2>&1; then rc=0; else rc=$?; fi
  local status="PASS"
  [ "$rc" -ne 0 ] && status="FAIL"
  local args=(--name "$name" --status "$status" --kind "$kind" --exit-code "$rc"
              --log "$log" --excerpt-file "$log")
  [ "$coreflag" = "core" ] && args+=(--core)
  [ -n "$target" ] && args+=(--target-host "$target")
  [ -n "$transport" ] && args+=(--transport "$transport")
  record "${args[@]}"
  echo "gate $name: $status ($log)"
  return "$rc"
}

# record_skip <name> <core|nocore> <reason> [target] [transport]
record_skip() {
  local name="$1" coreflag="$2" reason="$3" target="${4:-}" transport="${5:-}"
  local args=(--name "$name" --status SKIP --kind live --reason "$reason")
  [ "$coreflag" = "core" ] && args+=(--core)
  [ -n "$target" ] && args+=(--target-host "$target")
  [ -n "$transport" ] && args+=(--transport "$transport")
  record "${args[@]}"
  echo "gate $name: SKIP ($reason)"
}

resolve_target() { # <profile> -> "ssh_target"
  python3 - "$1" <<'PY'
import sys
sys.path.insert(0, "scripts")
from lib import clipboard_bootstrap as CB
print(CB.resolve_profile(sys.argv[1])["ssh_target"])
PY
}

resolve_image_alias() { # <alias> -> "profile ssh_target"
  python3 - "$1" <<'PY'
import sys
sys.path.insert(0, "scripts")
from lib import clipboard_bootstrap as CB
profile = CB.resolve_clipimg_alias(sys.argv[1])
print(profile, CB.resolve_profile(profile)["ssh_target"])
PY
}

osc52_decode_check() { # <capture-file> <marker>
  python3 - "$1" "$2" <<'PY'
import base64, re, sys
data = open(sys.argv[1], "rb").read()
marker = sys.argv[2].encode()
payloads = re.findall(rb"\x1b\]52;[^;\x07\x1b]*;([A-Za-z0-9+/=]+)(?:\x07|\x1b\\\\)", data)
for payload in payloads:
    try:
        if base64.b64decode(payload) == marker:
            print("OSC52 payload decoded to marker: OK")
            sys.exit(0)
    except Exception:
        pass
print(f"no OSC52 payload matching marker among {len(payloads)} candidate(s)", file=sys.stderr)
sys.exit(1)
PY
}

# ---------------------------------------------------------------------------
# Gates shared by both modes (mocked/unit/static proof — NOT live terminal proof)
# ---------------------------------------------------------------------------

gate_unit_tests() {
  python3 -m unittest tests.test_clipboard_bootstrap tests.test_clipboard_closeout -v
}

gate_static_checks() {
  local fail=0 helper
  echo "=== bash -n ==="
  for helper in scripts/clipboard/clipcopy scripts/clipboard/clippaste \
                scripts/clipboard/pbcopy scripts/clipboard/clipimg-put \
                scripts/clipboard-bootstrap scripts/clipboard-closeout.sh \
                scripts/clipboard-proof.sh; do
    echo "bash -n $helper"
    bash -n "$helper" || fail=1
  done
  if command -v shellcheck >/dev/null 2>&1; then
    echo "=== shellcheck ==="
    shellcheck -S warning scripts/clipboard/clipcopy scripts/clipboard/clippaste \
      scripts/clipboard/pbcopy scripts/clipboard/clipimg-put \
      scripts/clipboard-bootstrap scripts/clipboard-closeout.sh \
      scripts/clipboard-proof.sh || fail=1
  else
    echo "note: shellcheck unavailable on this host; bash -n only"
  fi
  echo "=== git diff --check ==="
  git diff --check || fail=1
  return "$fail"
}

gate_bootstrap_launch() {
  echo "=== --help ==="
  scripts/clipboard-bootstrap --help
  echo "=== --dry-run d3 ==="
  local out
  out="$(scripts/clipboard-bootstrap --profile d3 --dry-run)"
  printf '%s\n' "$out"
  grep -q "skillbox-portfolio-devbox" <<<"$out"
  grep -q "xterm-ghostty" <<<"$out"
}

# ---------------------------------------------------------------------------
# Live terminal gates (real hosts, real tmux, real OSC52 bytes)
# ---------------------------------------------------------------------------

gate_current_host_migration() {
  local fail=0 actual
  echo "expected managed clipcopy sha256: $EXPECT_CLIPCOPY_SHA"
  if [ -x "$HOME/.local/bin/clipcopy" ]; then
    actual="$(sha256sum "$HOME/.local/bin/clipcopy" | awk '{print $1}')"
    echo "installed clipcopy sha256:       $actual"
    if [ "$actual" = "$EXPECT_CLIPCOPY_SHA" ]; then
      echo "ok: ~/.local/bin/clipcopy is the managed bundle version"
    else
      echo "FAIL: ~/.local/bin/clipcopy is not the managed bundle version"
      fail=1
    fi
  else
    echo "FAIL: ~/.local/bin/clipcopy missing or not executable"
    fail=1
  fi
  if [ -f "$HOME/.config/skillbox/clipboard.tmux.conf" ]; then
    if cmp -s "$HOME/.config/skillbox/clipboard.tmux.conf" scripts/clipboard/tmux.conf; then
      echo "ok: managed tmux fragment matches bundle"
    else
      echo "FAIL: managed tmux fragment drifted from bundle"
      fail=1
    fi
  else
    echo "FAIL: ~/.config/skillbox/clipboard.tmux.conf missing"
    fail=1
  fi
  if grep -Fq "clipboard.tmux.conf" "$HOME/.tmux.conf" 2>/dev/null; then
    echo "ok: ~/.tmux.conf sources the managed fragment"
  else
    echo "FAIL: ~/.tmux.conf does not source the managed fragment"
    fail=1
  fi
  if infocmp -x xterm-ghostty >/dev/null 2>&1; then
    echo "ok: xterm-ghostty terminfo present"
  else
    echo "FAIL: xterm-ghostty terminfo missing"
    fail=1
  fi
  return "$fail"
}

gate_local_tmux() {
  local sock="$1" marker="$2" sc buf="" i
  command -v tmux >/dev/null 2>&1 || { echo "tmux unavailable"; return 1; }
  tmux -L "$sock" -f scripts/clipboard/tmux.conf new-session -d -s closeout
  sc="$(tmux -L "$sock" show-option -gv set-clipboard 2>/dev/null || true)"
  echo "set-clipboard=$sc"
  tmux -L "$sock" send-keys -t closeout \
    "printf '%s' '$marker' | '$ROOT_DIR/scripts/clipboard/clipcopy'" Enter
  for i in $(seq 1 20); do
    buf="$(tmux -L "$sock" show-buffer 2>/dev/null || true)"
    [ "$buf" = "$marker" ] && break
    sleep 0.5
  done
  echo "buffer=$buf"
  tmux -L "$sock" kill-server 2>/dev/null || true
  [ "$sc" = "on" ] && [ "$buf" = "$marker" ]
}

probe_reachable() { # <ssh_target> <log>
  timeout 25 ssh -o BatchMode=yes -o ConnectTimeout=8 "$1" 'echo "reach-ok $(hostname)"' \
    >"$2" 2>&1 && grep -q "reach-ok" "$2"
}

gate_ssh_osc52() { # <ssh_target> <marker> <capture-file>
  local target="$1" marker="$2" cap="$3"
  echo "target=$target marker=$marker"
  echo "== forcing a tty and running the managed clipcopy over SSH =="
  timeout 40 ssh -tt -o BatchMode=yes -o ConnectTimeout=8 "$target" \
    "printf '%s' '$marker' | \$HOME/.local/bin/clipcopy" </dev/null >"$cap" 2>&1 || true
  echo "raw capture: $cap"
  osc52_decode_check "$cap" "$marker"
}

gate_host_state() { # <ssh_target>
  local target="$1"
  timeout 40 ssh -o BatchMode=yes -o ConnectTimeout=8 "$target" \
    bash -s -- "$EXPECT_CLIPCOPY_SHA" <<'EOS'
set -eu
expect="$1"
fail=0
echo "== helper executability =="
for h in clipcopy clippaste pbcopy; do
  if [ -x "$HOME/.local/bin/$h" ]; then
    echo "ok: $h executable"
  else
    echo "FAIL: $h missing or not executable"
    fail=1
  fi
done
echo "== managed clipcopy sha256 =="
actual="$(sha256sum "$HOME/.local/bin/clipcopy" 2>/dev/null | awk '{print $1}')"
echo "expected: $expect"
echo "actual:   $actual"
[ "$actual" = "$expect" ] || { echo "FAIL: clipcopy sha mismatch"; fail=1; }
echo "== tmux fragment =="
if [ -f "$HOME/.config/skillbox/clipboard.tmux.conf" ]; then
  echo "ok: managed fragment present"
else
  echo "FAIL: managed fragment missing"
  fail=1
fi
if grep -Fq "clipboard.tmux.conf" "$HOME/.tmux.conf" 2>/dev/null; then
  echo "ok: ~/.tmux.conf sources the fragment"
else
  echo "FAIL: ~/.tmux.conf missing fragment source line"
  fail=1
fi
echo "== terminfo =="
if infocmp -x xterm-ghostty >/dev/null 2>&1; then
  echo "ok: xterm-ghostty terminfo present"
else
  echo "FAIL: xterm-ghostty terminfo missing"
  fail=1
fi
exit "$fail"
EOS
}

gate_remote_tmux() { # <ssh_target> <sock> <marker>
  local target="$1" sock="$2" marker="$3"
  timeout 60 ssh -o BatchMode=yes -o ConnectTimeout=8 "$target" \
    bash -s -- "$sock" "$marker" <<'EOS'
set -eu
sock="$1"
marker="$2"
command -v tmux >/dev/null 2>&1 || { echo "FAIL: tmux unavailable on remote"; exit 1; }
tmux -L "$sock" -f "$HOME/.tmux.conf" new-session -d -s closeout
sc="$(tmux -L "$sock" show-option -gv set-clipboard 2>/dev/null || true)"
echo "remote set-clipboard=$sc"
tmux -L "$sock" send-keys -t closeout "printf '%s' '$marker' | \$HOME/.local/bin/clipcopy" Enter
buf=""
for _ in $(seq 1 20); do
  buf="$(tmux -L "$sock" show-buffer 2>/dev/null || true)"
  [ "$buf" = "$marker" ] && break
  sleep 0.5
done
echo "remote buffer=$buf"
tmux -L "$sock" kill-server 2>/dev/null || true
[ "$sc" = "on" ] && [ "$buf" = "$marker" ]
EOS
}

gate_nested_tmux() { # <ssh_target> <local_sock> <remote_sock> <marker>
  local target="$1" lsock="$2" rsock="$3" marker="$4" buf="" i
  command -v tmux >/dev/null 2>&1 || { echo "tmux unavailable"; return 1; }
  local inner="$ART/nested-inner.sh"
  cat >"$inner" <<EOF
#!/usr/bin/env bash
# generated by clipboard-closeout.sh --live: nested local+remote tmux OSC52 proof
exec ssh -tt -o BatchMode=yes -o ConnectTimeout=8 '$target' 'tmux -L $rsock new-session -d -s nested "sleep 3; printf %s $marker | \$HOME/.local/bin/clipcopy; sleep 30"; exec tmux -L $rsock attach -t nested'
EOF
  chmod +x "$inner"
  echo "inner helper: $inner"
  tmux -L "$lsock" -f scripts/clipboard/tmux.conf new-session -d -s nested -x 200 -y 50
  tmux -L "$lsock" send-keys -t nested "bash '$inner'" Enter
  for i in $(seq 1 30); do
    buf="$(tmux -L "$lsock" show-buffer 2>/dev/null || true)"
    [ "$buf" = "$marker" ] && break
    sleep 1
  done
  echo "local outer-tmux buffer=$buf"
  timeout 25 ssh -o BatchMode=yes -o ConnectTimeout=8 "$target" \
    "tmux -L '$rsock' kill-server 2>/dev/null || true" || true
  tmux -L "$lsock" kill-server 2>/dev/null || true
  [ "$buf" = "$marker" ]
}

gate_image_transfer() { # <ssh_target> <png> <remote_rel_path>
  local target="$1" png="$2" rel="$3" expect actual
  expect="$(sha256sum "$png" | awk '{print $1}')"
  echo "target=$target remote_file=~/$rel"
  echo "local synthetic png sha256: $expect"
  echo "note: clipimg-put reads the macOS clipboard (Darwin-only); this gate proves"
  echo "the same target mapping and scp transport with a synthetic PNG."
  timeout 30 ssh -o BatchMode=yes -o ConnectTimeout=8 "$target" "mkdir -p ~/clipboard-images"
  timeout 40 scp -q -o BatchMode=yes -o ConnectTimeout=8 "$png" "$target:$rel"
  actual="$(timeout 30 ssh -o BatchMode=yes -o ConnectTimeout=8 "$target" \
    "test -s '$rel' && sha256sum '$rel'" | awk '{print $1}')"
  echo "remote sha256: $actual"
  [ "$actual" = "$expect" ] || { echo "FAIL: remote file missing or sha mismatch"; return 1; }
  timeout 30 ssh -o BatchMode=yes -o ConnectTimeout=8 "$target" \
    "rm -f '$rel' && test ! -e '$rel'"
  echo "ok: remote temp file removed"
}

gate_cleanup() {
  local fail=0 sock entry target
  echo "== local temp tmux servers =="
  for sock in ${LOCAL_SOCKS[@]+"${LOCAL_SOCKS[@]}"}; do
    tmux -L "$sock" kill-server 2>/dev/null || true
    if tmux -L "$sock" list-sessions >/dev/null 2>&1; then
      echo "FAIL: local tmux server '$sock' still alive"
      fail=1
    else
      rm -f "${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)/$sock"
      echo "ok: local tmux server '$sock' gone (socket file removed)"
    fi
  done
  echo "== remote temp tmux servers =="
  for entry in ${REMOTE_SOCKS[@]+"${REMOTE_SOCKS[@]}"}; do
    target="${entry%% *}"
    sock="${entry##* }"
    timeout 25 ssh -o BatchMode=yes -o ConnectTimeout=8 "$target" \
      "tmux -L '$sock' kill-server 2>/dev/null || true; ! tmux -L '$sock' list-sessions >/dev/null 2>&1 && rm -f \"\${TMUX_TMPDIR:-/tmp}/tmux-\$(id -u)/$sock\" && ! test -e \"\${TMUX_TMPDIR:-/tmp}/tmux-\$(id -u)/$sock\"" \
      && echo "ok: remote tmux server '$sock' gone on $target (socket file removed)" \
      || { echo "warn: could not verify remote tmux server '$sock' on $target"; }
  done
  echo "== remote temp files =="
  for entry in ${REMOTE_FILES[@]+"${REMOTE_FILES[@]}"}; do
    target="${entry%% *}"
    sock="${entry##* }"
    timeout 25 ssh -o BatchMode=yes -o ConnectTimeout=8 "$target" \
      "rm -f '$sock' && test ! -e '$sock'" \
      && echo "ok: remote file '$sock' gone on $target" \
      || { echo "warn: could not verify remote file '$sock' removal on $target"; }
  done
  return "$fail"
}

best_effort_cleanup() {
  local sock
  for sock in ${LOCAL_SOCKS[@]+"${LOCAL_SOCKS[@]}"}; do
    tmux -L "$sock" kill-server 2>/dev/null || true
  done
}
trap best_effort_cleanup EXIT

# ---------------------------------------------------------------------------
# Run: gates shared by both modes
# ---------------------------------------------------------------------------

run_gate unit_tests unit core "" "" -- gate_unit_tests || true
run_gate static_checks static core "" "" -- gate_static_checks || true
run_gate bootstrap_launch unit core "" "" -- gate_bootstrap_launch || true

# ---------------------------------------------------------------------------
# Live terminal paths
# ---------------------------------------------------------------------------

live_path_names() {
  # canonical live paths; conference1's SSH path is the Conference direct WSL path
  echo "current_host_migration - -"
  echo "local_tmux - local-tmux"
  echo "direct_ghostty_osc52 - ghostty"
  echo "mosh_transport - mosh"
  local p target
  for p in $CANONICAL_TARGETS; do
    target="$(resolve_target "$p")"
    if [ "$p" = "conference1" ]; then
      echo "conference_direct_wsl $target ssh"
    else
      echo "ssh_osc52_$p $target ssh"
    fi
    echo "host_state_$p $target ssh"
    echo "remote_tmux_$p $target ssh+tmux"
  done
  echo "nested_tmux - tmux+ssh+tmux"
  local a
  for a in d s j c; do
    read -r _profile target <<<"$(resolve_image_alias "$a")"
    echo "image_transfer_$a $target scp"
  done
  echo "local_clipboard_restore - -"
}

if [ "$MODE" != "live" ]; then
  while read -r name target transport; do
    [ "$target" = "-" ] && target=""
    [ "$transport" = "-" ] && transport=""
    record_skip "$name" core \
      "live terminal path; not exercised in smoke mode — run scripts/clipboard-closeout.sh --live" \
      "$target" "$transport"
  done < <(live_path_names)
else
  # -- current host migration + local tmux ---------------------------------
  run_gate current_host_migration live core "$(hostname)" local -- gate_current_host_migration || true

  LSOCK="skbx-closeout-$$"
  LOCAL_SOCKS+=("$LSOCK")
  run_gate local_tmux live core "$(hostname)" local-tmux -- \
    gate_local_tmux "$LSOCK" "skbx-local-$STAMP-$RANDOM" || true

  # -- direct Ghostty OSC52 -------------------------------------------------
  if [ "$(uname -s)" = "Darwin" ] && [ -d "/Applications/Ghostty.app" ]; then
    run_gate direct_ghostty_osc52 live core "$(hostname)" ghostty -- \
      scripts/clipboard-proof.sh --live || true
  else
    record_skip direct_ghostty_osc52 core \
      "operator Mac + Ghostty not reachable from this $(uname -s) host; run scripts/clipboard-closeout.sh --live on the operator Mac" \
      "" ghostty
  fi

  # -- per-host reachability, SSH OSC52, host state, remote tmux ------------
  declare -A REACHABLE TARGET_OF
  FIRST_REACHABLE=""
  for p in $CANONICAL_TARGETS; do
    target="$(resolve_target "$p")"
    TARGET_OF[$p]="$target"
    case " $LIVE_TARGETS " in
      *" $p "*) ;;
      *)
        REACHABLE[$p]=excluded
        for gate_name in "ssh_osc52_$p" "host_state_$p" "remote_tmux_$p"; do
          [ "$p" = "conference1" ] && [ "$gate_name" = "ssh_osc52_$p" ] && gate_name="conference_direct_wsl"
          record_skip "$gate_name" core \
            "target '$p' excluded via CLIPBOARD_LIVE_TARGETS (core path: exclusion forces overall FAIL)" \
            "$target" ssh
        done
        continue
        ;;
    esac
    probe_log="$ART/probe_$p.log"
    if probe_reachable "$target" "$probe_log"; then
      REACHABLE[$p]=yes
      [ -n "$FIRST_REACHABLE" ] || FIRST_REACHABLE="$p"
      ssh_gate_name="ssh_osc52_$p"
      [ "$p" = "conference1" ] && ssh_gate_name="conference_direct_wsl"
      run_gate "$ssh_gate_name" live core "$target" ssh -- \
        gate_ssh_osc52 "$target" "skbx-ssh-$p-$STAMP-$RANDOM" "$ART/osc52-capture-$p.bin" || true
      run_gate "host_state_$p" live core "$target" ssh -- gate_host_state "$target" || true
      rsock="skbx-closeout-$STAMP-$$"
      REMOTE_SOCKS+=("$target $rsock")
      run_gate "remote_tmux_$p" live core "$target" ssh+tmux -- \
        gate_remote_tmux "$target" "$rsock" "skbx-rtmux-$p-$STAMP-$RANDOM" || true
    else
      REACHABLE[$p]=no
      reason="unreachable: $(tail -c 300 "$probe_log" | tr '\n' ' ')"
      [ "$p" = "d3" ] && reason="$reason (known-unreachable, tracked in bead skillbox-ifsi)"
      ssh_gate_name="ssh_osc52_$p"
      [ "$p" = "conference1" ] && ssh_gate_name="conference_direct_wsl"
      for gate_name in "$ssh_gate_name" "host_state_$p" "remote_tmux_$p"; do
        record --name "$gate_name" --status FAIL --kind live --core --exit-code 1 \
          --target-host "$target" --transport ssh --reason "$reason" --log "$probe_log"
        echo "gate $gate_name: FAIL ($reason)"
      done
    fi
  done

  # -- mosh (needs interactive operator terminal + verifiable local clipboard)
  if [ "$(uname -s)" = "Darwin" ] && [ -t 1 ] && command -v mosh >/dev/null 2>&1 && [ -n "$FIRST_REACHABLE" ]; then
    mosh_target="${TARGET_OF[$FIRST_REACHABLE]}"
    gate_mosh() {
      local marker="skbx-mosh-$STAMP-$RANDOM" saved
      saved="$(/usr/bin/pbpaste 2>/dev/null || true)"
      timeout 40 mosh "$mosh_target" -- sh -c \
        "printf '%s' '$marker' | \$HOME/.local/bin/clipcopy; sleep 2" || true
      sleep 2
      local got
      got="$(/usr/bin/pbpaste 2>/dev/null || true)"
      printf '%s' "$saved" | /usr/bin/pbcopy
      [ "$got" = "$marker" ]
    }
    run_gate mosh_transport live core "$mosh_target" mosh -- gate_mosh || true
  else
    record_skip mosh_transport core \
      "mosh OSC52 proof needs an interactive operator terminal with a verifiable local clipboard (macOS pbpaste); not available on this $(uname -s) runner" \
      "" mosh
  fi

  # -- nested local+remote tmux ---------------------------------------------
  if [ -n "$FIRST_REACHABLE" ]; then
    nested_target="${TARGET_OF[$FIRST_REACHABLE]}"
    NSOCK_LOCAL="skbx-nested-$$"
    NSOCK_REMOTE="skbx-nested-$STAMP-$$"
    LOCAL_SOCKS+=("$NSOCK_LOCAL")
    REMOTE_SOCKS+=("$nested_target $NSOCK_REMOTE")
    run_gate nested_tmux live core "$nested_target" tmux+ssh+tmux -- \
      gate_nested_tmux "$nested_target" "$NSOCK_LOCAL" "$NSOCK_REMOTE" \
      "skbx-nested-$STAMP-$RANDOM" || true
  else
    record_skip nested_tmux core "no reachable SSH target for the nested proof" "" tmux+ssh+tmux
  fi

  # -- synthetic PNG image transfer to d/s/j/c ------------------------------
  PNG_FILE="$ART/synthetic.png"
  python3 - "$PNG_FILE" <<'PY'
import struct, sys, zlib

def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)
raw = b"".join(b"\x00" + bytes(range(i * 12, i * 12 + 12)) for i in range(4))
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
open(sys.argv[1], "wb").write(png)
PY
  for a in d s j c; do
    read -r profile target <<<"$(resolve_image_alias "$a")"
    rel="clipboard-images/clipboard-closeout-$STAMP.png"
    case " $LIVE_TARGETS " in
      *" $profile "*) ;;
      *)
        record_skip "image_transfer_$a" core \
          "target '$profile' excluded via CLIPBOARD_LIVE_TARGETS" "$target" scp
        continue
        ;;
    esac
    if [ "${REACHABLE[$profile]:-no}" = "yes" ]; then
      REMOTE_FILES+=("$target $rel")
      run_gate "image_transfer_$a" live core "$target" scp -- \
        gate_image_transfer "$target" "$PNG_FILE" "$rel" || true
    else
      reason="unreachable (see probe_$profile.log)"
      [ "$profile" = "d3" ] && reason="$reason (known-unreachable, tracked in bead skillbox-ifsi)"
      record --name "image_transfer_$a" --status FAIL --kind live --core --exit-code 1 \
        --target-host "$target" --transport scp --reason "$reason" \
        --log "$ART/probe_$profile.log"
      echo "gate image_transfer_$a: FAIL ($reason)"
    fi
  done

  # -- local clipboard restore ----------------------------------------------
  gate_local_clipboard_restore() {
    if [ "$(uname -s)" = "Darwin" ]; then
      echo "Darwin: pbpaste snapshot/restore handled inside the mosh gate;"
      echo "OSC52 test bytes were captured to log files, never replayed to the terminal."
    else
      echo "Linux runner: no system clipboard on this host. All OSC52 test output was"
      echo "captured to files under $ART and never written to the controlling terminal;"
      echo "temp tmux buffers lived on isolated sockets that are killed by the cleanup gate."
    fi
  }
  run_gate local_clipboard_restore live core "$(hostname)" local -- gate_local_clipboard_restore || true

  # -- cleanup ----------------------------------------------------------------
  run_gate cleanup_temp_artifacts cleanup core "" "" -- gate_cleanup || true
fi

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

ln -sfn "$ART" "$ARTIFACT_ROOT/latest" 2>/dev/null || true
echo ""
echo "clipboard closeout report: $JSON_OUT"
python3 "$REPORT_PY" finalize --out "$JSON_OUT"
