#!/usr/bin/env bash
# Clipboard bootstrap closeout: unit gates + static checks + bootstrap launch proof.
set -euo pipefail

SCRIPT_SOURCE="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd -P)"
ROOT_DIR="${SKILLBOX_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"
SCRATCH="${CLIPBOARD_SCRATCH:-/tmp/grok-goal-ad25677da053/implementer}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
JSON_OUT="$SCRATCH/clipboard-closeout.json"

mkdir -p "$SCRATCH"

run_gate() {
  local name="$1"
  shift
  local stdout_file stderr_file rc
  stdout_file="$(mktemp)"
  stderr_file="$(mktemp)"
  if "$@" >"$stdout_file" 2>"$stderr_file"; then
    rc=0
  else
    rc=$?
  fi
  python3 - "$JSON_OUT" "$name" "$rc" "$stdout_file" "$stderr_file" <<'PY'
import json
import sys
from pathlib import Path

out, name, rc, stdout_path, stderr_path = sys.argv[1:6]
rc = int(rc)
stdout = Path(stdout_path).read_text(encoding="utf-8", errors="replace")
stderr = Path(stderr_path).read_text(encoding="utf-8", errors="replace")
path = Path(out)
payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"generated_at": "", "gates": []}
status = "PASS" if rc == 0 else "FAIL"
payload["gates"].append({
    "name": name,
    "status": status,
    "exit_code": rc,
    "stdout": stdout,
    "stderr": stderr,
})
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
  rm -f "$stdout_file" "$stderr_file"
  return "$rc"
}

printf '{"generated_at":"%s","gates":[]}\n' "$STAMP" >"$JSON_OUT"

overall=0

if ! run_gate "unit_tests" python3 -m unittest tests.test_clipboard_bootstrap -v; then
  overall=1
fi

static_log="$SCRATCH/clipboard-static-checks.log"
set +e
{
  echo "=== bash -n helpers ==="
  static_fail=0
  for helper in "$ROOT_DIR/scripts/clipboard"/{clipcopy,clippaste,pbcopy,clipimg-put} "$ROOT_DIR/scripts/clipboard-bootstrap" "$ROOT_DIR/scripts/clipboard-closeout.sh" "$ROOT_DIR/scripts/clipboard-proof.sh"; do
    echo "bash -n $helper"
    if ! bash -n "$helper"; then
      static_fail=1
    fi
  done
  echo "=== git diff --check ==="
  if ! (cd "$ROOT_DIR" && git diff --check); then
    static_fail=1
  fi
  exit "$static_fail"
} >"$static_log" 2>&1
static_rc=$?
set -e
python3 - "$JSON_OUT" "static_checks" "$static_rc" "$static_log" <<'PY'
import json
import sys
from pathlib import Path

out, name, rc, log_path = sys.argv[1:5]
rc = int(rc)
text = Path(log_path).read_text(encoding="utf-8", errors="replace")
path = Path(out)
payload = json.loads(path.read_text(encoding="utf-8"))
payload["gates"].append({
    "name": name,
    "status": "PASS" if rc == 0 else "FAIL",
    "exit_code": rc,
    "stdout": text,
    "stderr": "",
})
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
if [ "$static_rc" -ne 0 ]; then
  overall=1
fi

launch_log="$SCRATCH/clipboard-bootstrap-launch.log"
{
  echo "=== --help ==="
  "$ROOT_DIR/scripts/clipboard-bootstrap" --help
  echo "=== --dry-run d3 ==="
  "$ROOT_DIR/scripts/clipboard-bootstrap" --profile d3 --dry-run
} >"$launch_log" 2>&1
launch_rc=0
if ! grep -q "skillbox-portfolio-devbox" "$launch_log"; then launch_rc=1; fi
if ! grep -q "xterm-ghostty" "$launch_log"; then launch_rc=1; fi
python3 - "$JSON_OUT" "bootstrap_launch" "$launch_rc" "$launch_log" <<'PY'
import json
import sys
from pathlib import Path

out, name, rc, log_path = sys.argv[1:5]
rc = int(rc)
text = Path(log_path).read_text(encoding="utf-8", errors="replace")
path = Path(out)
payload = json.loads(path.read_text(encoding="utf-8"))
payload["gates"].append({
    "name": name,
    "status": "PASS" if rc == 0 else "FAIL",
    "exit_code": rc,
    "stdout": text,
    "stderr": "",
})
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
if [ "$launch_rc" -ne 0 ]; then
  overall=1
fi

live_reason="non-Darwin runner or Ghostty unavailable"
if [ "$(uname -s)" = "Darwin" ] && [ -d "/Applications/Ghostty.app" ]; then
  live_status="SKIP"
  live_reason="live Ghostty proof requires operator manual run: scripts/clipboard-proof.sh --live"
else
  live_status="SKIP"
fi
python3 - "$JSON_OUT" "$live_status" "$live_reason" <<'PY'
import json
import sys
from pathlib import Path

out, status, reason = sys.argv[1:4]
path = Path(out)
payload = json.loads(path.read_text(encoding="utf-8"))
payload["gates"].append({
    "name": "live_ghostty_proof",
    "status": status,
    "reason": reason,
})
payload["overall"] = "PASS" if all(
    g.get("status") == "PASS" for g in payload["gates"] if g["status"] != "SKIP"
) else "FAIL"
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY

echo "clipboard closeout: $JSON_OUT"
if [ "$overall" -ne 0 ]; then
  exit 1
fi
exit 0