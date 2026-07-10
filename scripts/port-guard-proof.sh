#!/usr/bin/env bash
# Run the port-guard proof harness and write a dated local report.

set -euo pipefail

SCRIPT_SOURCE="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd -P)"
ROOT_DIR="${SKILLBOX_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${PORT_GUARD_PROOF_DIR:-$ROOT_DIR/agent_ergonomics_audit}"
REPORT="$OUT_DIR/port-guard-proof-$STAMP.md"
JSON_REPORT="$OUT_DIR/port-guard-proof-$STAMP.json"

mkdir -p "$OUT_DIR"

proof_json="$(
  test_output_file="/tmp/port-guard-proof-tests.$$"
  if python3 -m unittest tests.test_port_guard_regression -v >"$test_output_file" 2>&1; then
    test_status=0
  else
    test_status=$?
  fi
  test_output="$(cat "$test_output_file")"
  rm -f "$test_output_file"
  python3 - "$STAMP" "$test_status" "$test_output" "$ROOT_DIR" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

stamp, test_status, test_output, root = sys.argv[1], int(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
criteria_labels = {
    "CRITERION_1": "direct dev command blocks with exact remediation",
    "CRITERION_2": "bypassed dev signatures are reaped after grace",
    "CRITERION_3": "post-bind verification catches silent port hops",
    "CRITERION_4": "counters survive pulse restart hydration",
    "CRITERION_5": "tailnet-only wildcard listeners are rejected",
}

def criterion_status(marker):
    lines = [line for line in test_output.splitlines() if marker in line and "test_" in line]
    if not lines:
        return "UNKNOWN"
    return "PASS" if all(line.rstrip().endswith("ok") for line in lines) else "FAIL"

def run(name, args):
    proc = subprocess.run(args, cwd=root, text=True, capture_output=True, check=False, timeout=60)
    return {
        "name": name,
        "status": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }

payload = {
    "generated_at": stamp,
    "tests": {"status": test_status, "output": test_output},
    "criteria": {
        marker: {"status": criterion_status(marker), "label": label}
        for marker, label in criteria_labels.items()
    },
    "commands": [
        run("ports", ["python3", ".env-manager/manage.py", "ports", "--format", "json"]),
        run("doctor", ["python3", ".env-manager/manage.py", "doctor", "--format", "json"]),
        run("pulse-status", ["python3", ".env-manager/pulse.py", "--root-dir", str(root), "status"]),
    ],
}
print(json.dumps(payload, indent=2))
PY
)"

printf '%s\n' "$proof_json" > "$JSON_REPORT"

python3 - "$JSON_REPORT" "$REPORT" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
report = Path(sys.argv[2])
test_status = payload["tests"]["status"]
lines = [
    f"# Port Guard Proof {payload['generated_at']}",
    "",
    f"- Criterion regression suite: {'PASS' if test_status == 0 else 'FAIL'}",
    "- Criteria covered:",
]
for marker, detail in payload.get("criteria", {}).items():
    lines.append(f"  - {marker}: {detail['status']} - {detail['label']}")
lines.extend([
    "",
    "## Regression Output",
    "",
    "```text",
    payload["tests"]["output"].rstrip(),
    "```",
])
for command in payload["commands"]:
    status = command["status"]
    lines.extend(
        [
            "",
            f"## {command['name']} ({'PASS' if status == 0 else 'FAIL'})",
            "",
            "```text",
            (command.get("stdout") or command.get("stderr") or "").rstrip(),
            "```",
        ]
    )
report.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
PY

echo "port-guard proof: $REPORT"
echo "port-guard proof json: $JSON_REPORT"
