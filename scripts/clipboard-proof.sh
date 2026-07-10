#!/usr/bin/env bash
# Optional live Ghostty/Mac clipboard proof. Unit gates remain the CI bar.
set -euo pipefail

SCRIPT_SOURCE="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd -P)"
ROOT_DIR="${SKILLBOX_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"

live="false"
while [ $# -gt 0 ]; do
  case "$1" in
    --live) live="true"; shift ;;
    -h|--help)
      cat <<EOF
usage: clipboard-proof.sh [--live]

Without --live: runs unit/fixture proof via clipboard-closeout.sh.
With --live: requires Darwin + Ghostty; launches disposable Ghostty windows and
polls /usr/bin/pbpaste for OSC52 proof (operator environment only).
EOF
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ "$live" != "true" ]; then
  exec "$ROOT_DIR/scripts/clipboard-closeout.sh"
fi

if [ "$(uname -s)" != "Darwin" ]; then
  echo "SKIP: live proof requires macOS" >&2
  exit 0
fi

if [ ! -d "/Applications/Ghostty.app" ]; then
  echo "SKIP: Ghostty.app not found" >&2
  exit 0
fi

marker="skillbox-clipboard-proof-$(date +%s)"
open -na Ghostty.app --args --clipboard-write=allow --confirm-close-surface=false -e bash -lc \
  "printf '%s' '$marker' | $HOME/.local/bin/clipcopy; sleep 2"
sleep 3
if /usr/bin/pbpaste | grep -Fq "$marker"; then
  echo "PASS: Ghostty OSC52 direct write"
  exit 0
fi
echo "FAIL: Ghostty OSC52 proof marker not in pbpaste" >&2
exit 1