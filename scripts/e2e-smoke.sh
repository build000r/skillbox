#!/usr/bin/env bash
#
# Opt-in local e2e smoke. This intentionally stays out of default CI: it checks
# process boundaries and loopback HTTP stubs without Docker up or durable writes.

set -uo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/e2e-smoke.sh [--format text|json] [--strict]

Runs a read-only end-to-end smoke:
  render, doctor, runtime-render, sync --dry-run, compose config, and live
  API/web stub probes on ephemeral loopback ports.

Options:
  --format text|json  Output a human table or machine-readable JSON summary.
  --json              Alias for --format json.
  --strict            Treat doctor failures as smoke failures.
  -h, --help          Show this help.

Environment:
  SKILLBOX_E2E_ROOT_DIR              Override repo root for tests.
  SKILLBOX_E2E_STEP_TIMEOUT_SECONDS  Per-command timeout, default 15.
  SKILLBOX_E2E_STUB_TIMEOUT_SECONDS  Stub readiness timeout, default 10.
  SKILLBOX_E2E_API_PORT              Force API port instead of ephemeral.
  SKILLBOX_E2E_WEB_PORT              Force web port instead of ephemeral.
  SKILLBOX_E2E_MTIME_ROOTS           Colon-separated watched paths.
EOF
}

ROOT_DIR_DEFAULT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT_DIR="${SKILLBOX_E2E_ROOT_DIR:-$ROOT_DIR_DEFAULT}"
ROOT_DIR="$(cd "$ROOT_DIR" && pwd)"
PYTHON_BIN="${PYTHON:-python3}"
FORMAT="text"
STRICT=0
STEP_TIMEOUT_SECONDS="${SKILLBOX_E2E_STEP_TIMEOUT_SECONDS:-15}"
STUB_TIMEOUT_SECONDS="${SKILLBOX_E2E_STUB_TIMEOUT_SECONDS:-10}"
BIND_HOST="127.0.0.1"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --format)
      if [[ $# -lt 2 ]]; then
        echo "e2e-smoke: --format requires text or json" >&2
        exit 2
      fi
      FORMAT="$2"
      shift 2
      ;;
    --json)
      FORMAT="json"
      shift
      ;;
    --strict)
      STRICT=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "e2e-smoke: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$FORMAT" != "text" && "$FORMAT" != "json" ]]; then
  echo "e2e-smoke: --format must be text or json" >&2
  exit 2
fi

TMP_DIR="$(mktemp -d)"
RESULTS_FILE="$TMP_DIR/results.jsonl"
MTIME_BEFORE="$TMP_DIR/mtimes.before.json"
MTIME_DIFF="$TMP_DIR/mtime.diff.json"
START_NS="$(date +%s%N)"
API_PID=""
WEB_PID=""
API_PORT=""
WEB_PORT=""
FAIL_COUNT=0
STEP_DETAIL=""
STEP_COMMAND=""

: >"$RESULTS_FILE"

stop_pid() {
  local pid="$1"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
}

cleanup() {
  local code=$?
  trap - EXIT
  stop_pid "$WEB_PID"
  stop_pid "$API_PID"
  rm -rf "$TMP_DIR"
  exit "$code"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

now_ns() {
  date +%s%N
}

run_with_timeout() {
  if command -v timeout >/dev/null 2>&1; then
    timeout "${STEP_TIMEOUT_SECONDS}s" "$@"
  else
    "$@"
  fi
}

summarize_output() {
  local err_file="$1"
  local out_file="$2"
  local source_file="$err_file"
  if [[ ! -s "$source_file" ]]; then
    source_file="$out_file"
  fi
  if [[ -s "$source_file" ]]; then
    tr '\n' ' ' <"$source_file" | sed 's/[[:space:]][[:space:]]*/ /g' | cut -c1-240
  else
    printf "no diagnostic output"
  fi
}

append_result() {
  local name="$1"
  local status="$2"
  local exit_code="$3"
  local elapsed_ms="$4"
  local detail="$5"
  local command="$6"
  local out_file="$7"
  local err_file="$8"

  RESULTS_FILE="$RESULTS_FILE" \
  STEP_NAME="$name" \
  STEP_STATUS="$status" \
  STEP_EXIT_CODE="$exit_code" \
  STEP_ELAPSED_MS="$elapsed_ms" \
  STEP_DETAIL="$detail" \
  STEP_COMMAND="$command" \
  STEP_STDOUT="$out_file" \
  STEP_STDERR="$err_file" \
    "$PYTHON_BIN" - <<'PY'
import json
import os

record = {
    "name": os.environ["STEP_NAME"],
    "status": os.environ["STEP_STATUS"],
    "exit_code": int(os.environ["STEP_EXIT_CODE"]),
    "elapsed_ms": int(os.environ["STEP_ELAPSED_MS"]),
    "detail": os.environ["STEP_DETAIL"],
    "command": os.environ["STEP_COMMAND"],
    "stdout": os.environ["STEP_STDOUT"],
    "stderr": os.environ["STEP_STDERR"],
}
with open(os.environ["RESULTS_FILE"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, sort_keys=True) + "\n")
PY
}

run_step() {
  local name="$1"
  local severity="$2"
  local func="$3"
  local safe_name="${name//[^A-Za-z0-9_]/_}"
  local out_file="$TMP_DIR/${safe_name}.out"
  local err_file="$TMP_DIR/${safe_name}.err"
  local start_ns end_ns elapsed_ms rc status detail command

  STEP_DETAIL=""
  STEP_COMMAND="$func"
  start_ns="$(now_ns)"
  if "$func" >"$out_file" 2>"$err_file"; then
    rc=0
  else
    rc=$?
  fi
  end_ns="$(now_ns)"
  elapsed_ms=$(((end_ns - start_ns) / 1000000))

  status="PASS"
  if [[ "$rc" -ne 0 ]]; then
    status="FAIL"
  fi
  if [[ "$status" == "FAIL" && "$severity" == "doctor" && "$STRICT" -ne 1 ]]; then
    status="WARN"
  fi

  detail="$STEP_DETAIL"
  if [[ -z "$detail" ]]; then
    if [[ "$rc" -eq 124 ]]; then
      detail="timed out after ${STEP_TIMEOUT_SECONDS}s"
    elif [[ "$status" == "PASS" ]]; then
      detail="ok"
    else
      detail="$(summarize_output "$err_file" "$out_file")"
    fi
  fi
  command="$STEP_COMMAND"

  append_result "$name" "$status" "$rc" "$elapsed_ms" "$detail" "$command" "$out_file" "$err_file"
  if [[ "$status" == "FAIL" ]]; then
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

require_json_keys() {
  local json_file="$1"
  shift
  "$PYTHON_BIN" - "$json_file" "$@" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
keys = sys.argv[2:]
with path.open(encoding="utf-8") as handle:
    payload = json.load(handle)
if not isinstance(payload, dict):
    raise SystemExit(f"{path} is not a JSON object")
missing = [key for key in keys if key not in payload]
if missing:
    raise SystemExit(f"{path} missing keys: {', '.join(missing)}")
PY
}

summarize_doctor_json() {
  local json_file="$1"
  "$PYTHON_BIN" - "$json_file" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
with path.open(encoding="utf-8") as handle:
    payload = json.load(handle)
if not isinstance(payload, list):
    raise SystemExit("doctor JSON is not a list")
counts = {"pass": 0, "warn": 0, "fail": 0}
for row in payload:
    if not isinstance(row, dict):
        raise SystemExit("doctor JSON contains a non-object row")
    status = str(row.get("status", ""))
    if status in counts:
        counts[status] += 1
print(f"doctor checks pass={counts['pass']} warn={counts['warn']} fail={counts['fail']}")
raise SystemExit(1 if counts["fail"] else 0)
PY
}

snapshot_mtimes() {
  local output_file="$1"
  "$PYTHON_BIN" - "$ROOT_DIR" "$output_file" <<'PY'
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2])
default_roots = [
    "workspace",
    ".skillbox",
    ".skillbox-state",
    "logs",
    "home",
    "repos",
    "skills",
    ".env-manager",
    "scripts",
    "docker-compose.yml",
    "docker-compose.monoserver.yml",
    "Makefile",
    "README.md",
]
raw_roots = os.environ.get("SKILLBOX_E2E_MTIME_ROOTS")
watch_roots = raw_roots.split(os.pathsep) if raw_roots else default_roots
exclude_dirs = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
records: dict[str, dict[str, int | str]] = {}

def add_path(path: Path) -> None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return
    rel = path.relative_to(root).as_posix()
    records[rel] = {
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
        "kind": "dir" if path.is_dir() else "file",
    }

for raw in watch_roots:
    if not raw:
        continue
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        path = path.resolve()
        path.relative_to(root)
    except (FileNotFoundError, ValueError):
        continue
    if not path.exists():
        continue
    add_path(path)
    if path.is_dir():
        for current, dirs, files in os.walk(path):
            dirs[:] = [name for name in dirs if name not in exclude_dirs]
            current_path = Path(current)
            add_path(current_path)
            for filename in files:
                add_path(current_path / filename)

output.write_text(json.dumps(records, sort_keys=True), encoding="utf-8")
PY
}

compare_mtimes() {
  local before_file="$1"
  local output_file="$2"
  local after_file="$TMP_DIR/mtimes.after.json"
  snapshot_mtimes "$after_file"
  "$PYTHON_BIN" - "$before_file" "$after_file" "$output_file" <<'PY'
import json
import sys
from pathlib import Path

before = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
after = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
changed = []
for rel in sorted(set(before) | set(after)):
    old = before.get(rel)
    new = after.get(rel)
    if old != new:
        if old is None:
            reason = "added"
        elif new is None:
            reason = "removed"
        else:
            reason = "metadata_changed"
        changed.append({"path": rel, "reason": reason, "before": old, "after": new})
payload = {"ok": not changed, "count": len(changed), "changed": changed[:100]}
Path(sys.argv[3]).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
raise SystemExit(0 if payload["ok"] else 1)
PY
}

choose_port() {
  local override_name="$1"
  local override="${!override_name:-}"
  if [[ -n "$override" ]]; then
    printf "%s\n" "$override"
    return 0
  fi
  "$PYTHON_BIN" - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

wait_for_http() {
  local url="$1"
  local pid="$2"
  "$PYTHON_BIN" - "$url" "$pid" "$STUB_TIMEOUT_SECONDS" <<'PY'
import os
import sys
import time
import urllib.request

url = sys.argv[1]
pid = int(sys.argv[2])
timeout = float(sys.argv[3])
deadline = time.monotonic() + timeout
last_error = "not attempted"
while time.monotonic() < deadline:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        raise SystemExit(f"process {pid} exited before {url} became ready")
    except PermissionError:
        pass
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            response.read()
        raise SystemExit(0)
    except Exception as exc:  # noqa: BLE001 - diagnostic for smoke output
        last_error = str(exc)
        time.sleep(0.1)
raise SystemExit(f"timeout waiting for {url}: {last_error}")
PY
}

probe_api() {
  local port="$1"
  "$PYTHON_BIN" - "$port" "$STUB_TIMEOUT_SECONDS" <<'PY'
import json
import sys
import urllib.request

port = int(sys.argv[1])
timeout = float(sys.argv[2])
base = f"http://127.0.0.1:{port}"

def fetch_json(path: str) -> object:
    with urllib.request.urlopen(base + path, timeout=timeout) as response:
        if response.status != 200:
            raise SystemExit(f"{path} returned {response.status}")
        return json.loads(response.read().decode("utf-8"))

health = fetch_json("/health")
if not isinstance(health, dict) or health.get("ok") is not True:
    raise SystemExit("/health missing ok=true")
sandbox = fetch_json("/v1/sandbox")
if not isinstance(sandbox, dict) or "runtime_manager" not in sandbox:
    raise SystemExit("/v1/sandbox missing runtime_manager")
runtime = fetch_json("/v1/runtime")
required = {"manifest", "repos", "skills", "services", "logs", "checks"}
if not isinstance(runtime, dict) or not required.issubset(runtime):
    missing = sorted(required.difference(runtime if isinstance(runtime, dict) else {}))
    raise SystemExit(f"/v1/runtime missing keys: {', '.join(missing)}")
PY
}

probe_web() {
  local port="$1"
  "$PYTHON_BIN" - "$port" <<'PY'
import sys
import urllib.request

port = int(sys.argv[1])
with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2.0) as response:
    body = response.read().decode("utf-8")
if response.status != 200:
    raise SystemExit(f"web root returned {response.status}")
if "<h1>skillbox</h1>" not in body:
    raise SystemExit("web root missing skillbox heading")
PY
}

compose_env_args() {
  local state_root="${SKILLBOX_STATE_ROOT:-./.skillbox-state}"
  local state_path
  if [[ "$state_root" = /* ]]; then
    state_path="$state_root"
  else
    state_path="$ROOT_DIR/$state_root"
  fi
  if [[ -f "$state_path/operator/.env" ]]; then
    printf "%s\n%s\n" "--env-file" "$state_path/operator/.env"
  elif [[ -f "$ROOT_DIR/.env" ]]; then
    printf "%s\n%s\n" "--env-file" "$ROOT_DIR/.env"
  fi
}

step_render() {
  local json_file="$TMP_DIR/render.json"
  STEP_COMMAND="python3 scripts/04-reconcile.py render --format json"
  run_with_timeout "$PYTHON_BIN" "$ROOT_DIR/scripts/04-reconcile.py" render --format json >"$json_file"
  local rc=$?
  if [[ "$rc" -eq 0 ]]; then
    require_json_keys "$json_file" sandbox runtime_manager expected_files expected_mounts || return $?
    STEP_DETAIL="outer render JSON shape ok"
  fi
  return "$rc"
}

step_doctor() {
  local json_file="$TMP_DIR/doctor.json"
  local parse_rc=0
  STEP_COMMAND="python3 scripts/04-reconcile.py doctor --format json"
  run_with_timeout "$PYTHON_BIN" "$ROOT_DIR/scripts/04-reconcile.py" doctor --format json >"$json_file"
  local rc=$?
  if [[ -s "$json_file" ]]; then
    STEP_DETAIL="$(summarize_doctor_json "$json_file" 2>/dev/null)" || parse_rc=$?
  fi
  if [[ "$rc" -ne 0 ]]; then
    return "$rc"
  fi
  return "$parse_rc"
}

step_runtime_render() {
  local json_file="$TMP_DIR/runtime-render.json"
  STEP_COMMAND="python3 .env-manager/manage.py render --format json"
  run_with_timeout "$PYTHON_BIN" "$ROOT_DIR/.env-manager/manage.py" render --format json >"$json_file"
  local rc=$?
  if [[ "$rc" -eq 0 ]]; then
    require_json_keys "$json_file" root_dir repos skills services logs checks || return $?
    STEP_DETAIL="runtime render JSON shape ok"
  fi
  return "$rc"
}

step_sync_dry_run() {
  local json_file="$TMP_DIR/sync-dry-run.json"
  STEP_COMMAND="python3 .env-manager/manage.py sync --dry-run --format json"
  run_with_timeout "$PYTHON_BIN" "$ROOT_DIR/.env-manager/manage.py" sync --dry-run --format json >"$json_file"
  local rc=$?
  if [[ "$rc" -eq 0 ]]; then
    require_json_keys "$json_file" actions dry_run || return $?
    "$PYTHON_BIN" - "$json_file" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
if payload.get("dry_run") is not True:
    raise SystemExit("sync payload missing dry_run=true")
PY
    local dry_run_rc=$?
    if [[ "$dry_run_rc" -ne 0 ]]; then
      return "$dry_run_rc"
    fi
    STEP_DETAIL="runtime sync dry-run JSON shape ok"
  fi
  return "$rc"
}

step_compose_config() {
  local compose_file="$ROOT_DIR/docker-compose.yml"
  local layer_file="$ROOT_DIR/docker-compose.monoserver.yml"
  local env_args=()
  local line
  STEP_COMMAND="docker compose -f docker-compose.yml -f docker-compose.monoserver.yml config -q"
  while IFS= read -r line; do
    env_args+=("$line")
  done < <(compose_env_args)
  (
    cd "$ROOT_DIR" || exit 1
    run_with_timeout docker compose "${env_args[@]}" -f "$compose_file" -f "$layer_file" --profile surfaces config -q
  )
  local rc=$?
  if [[ "$rc" -eq 0 ]]; then
    STEP_DETAIL="default and surfaces compose config resolved"
  fi
  return "$rc"
}

step_stub_api() {
  API_PORT="$(choose_port SKILLBOX_E2E_API_PORT)"
  STEP_COMMAND="SKILLBOX_API_HOST=127.0.0.1 SKILLBOX_API_PORT=${API_PORT} python3 scripts/stub_api.py"
  (
    cd "$ROOT_DIR" || exit 1
    PYTHONDONTWRITEBYTECODE=1 \
    SKILLBOX_API_HOST="$BIND_HOST" \
    SKILLBOX_API_PORT="$API_PORT" \
      exec "$PYTHON_BIN" scripts/stub_api.py
  ) >"$TMP_DIR/stub-api.log" 2>&1 &
  API_PID=$!
  wait_for_http "http://127.0.0.1:${API_PORT}/health" "$API_PID" &&
    probe_api "$API_PORT"
  local rc=$?
  if [[ "$rc" -eq 0 ]]; then
    STEP_DETAIL="api stub healthy on 127.0.0.1:${API_PORT}"
  else
    STEP_DETAIL="$(summarize_output "$TMP_DIR/stub-api.log" "$TMP_DIR/stub-api.log")"
  fi
  return "$rc"
}

step_stub_web() {
  WEB_PORT="$(choose_port SKILLBOX_E2E_WEB_PORT)"
  STEP_COMMAND="SKILLBOX_WEB_HOST=127.0.0.1 SKILLBOX_WEB_PORT=${WEB_PORT} python3 scripts/stub_web.py"
  (
    cd "$ROOT_DIR" || exit 1
    PYTHONDONTWRITEBYTECODE=1 \
    SKILLBOX_WEB_HOST="$BIND_HOST" \
    SKILLBOX_WEB_PORT="$WEB_PORT" \
    SKILLBOX_API_PORT="${API_PORT:-8000}" \
      exec "$PYTHON_BIN" scripts/stub_web.py
  ) >"$TMP_DIR/stub-web.log" 2>&1 &
  WEB_PID=$!
  wait_for_http "http://127.0.0.1:${WEB_PORT}/" "$WEB_PID"
  local wait_rc=$?
  local rc="$wait_rc"
  if [[ "$wait_rc" -eq 0 ]]; then
    probe_web "$WEB_PORT"
    rc=$?
  fi
  if [[ "$rc" -eq 0 ]]; then
    STEP_DETAIL="web stub healthy on 127.0.0.1:${WEB_PORT}"
  else
    STEP_DETAIL="$(summarize_output "$TMP_DIR/stub-web.log" "$TMP_DIR/stub-web.log")"
  fi
  return "$rc"
}

step_cleanup_processes() {
  STEP_COMMAND="terminate live stubs"
  stop_pid "$WEB_PID"
  WEB_PID=""
  stop_pid "$API_PID"
  API_PID=""
  STEP_DETAIL="stub processes stopped"
  return 0
}

step_state_mutation() {
  STEP_COMMAND="compare watched mtime snapshot"
  if compare_mtimes "$MTIME_BEFORE" "$MTIME_DIFF"; then
    STEP_DETAIL="no watched mtime changes"
    return 0
  fi
  local count
  count="$("$PYTHON_BIN" - "$MTIME_DIFF" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("count", 0))
PY
)"
  STEP_DETAIL="${count} watched path(s) changed"
  return 1
}

emit_summary() {
  local end_ns elapsed_ms
  end_ns="$(now_ns)"
  elapsed_ms=$(((end_ns - START_NS) / 1000000))
  SUMMARY_FORMAT="$FORMAT" \
  ROOT_DIR="$ROOT_DIR" \
  STRICT="$STRICT" \
  RESULTS_FILE="$RESULTS_FILE" \
  MTIME_DIFF="$MTIME_DIFF" \
  API_PORT="$API_PORT" \
  WEB_PORT="$WEB_PORT" \
  BIND_HOST="$BIND_HOST" \
  ELAPSED_MS="$elapsed_ms" \
    "$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path

results_path = Path(os.environ["RESULTS_FILE"])
steps = [
    json.loads(line)
    for line in results_path.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
for step in steps:
    counts[step["status"]] = counts.get(step["status"], 0) + 1
diff_path = Path(os.environ["MTIME_DIFF"])
state_mutation = {"ok": True, "count": 0, "changed": []}
if diff_path.exists():
    state_mutation = json.loads(diff_path.read_text(encoding="utf-8"))
payload = {
    "ok": counts.get("FAIL", 0) == 0,
    "root_dir": os.environ["ROOT_DIR"],
    "strict": os.environ["STRICT"] == "1",
    "elapsed_ms": int(os.environ["ELAPSED_MS"]),
    "counts": {
        "pass": counts.get("PASS", 0),
        "warn": counts.get("WARN", 0),
        "fail": counts.get("FAIL", 0),
    },
    "ports": {
        "host": os.environ["BIND_HOST"],
        "api": int(os.environ["API_PORT"]) if os.environ.get("API_PORT") else None,
        "web": int(os.environ["WEB_PORT"]) if os.environ.get("WEB_PORT") else None,
    },
    "state_mutation": state_mutation,
    "steps": steps,
}
if os.environ["SUMMARY_FORMAT"] == "json":
    print(json.dumps(payload, indent=2, sort_keys=True))
else:
    print("skillbox e2e smoke")
    print(f"root: {payload['root_dir']}")
    print(f"strict: {str(payload['strict']).lower()}  elapsed: {payload['elapsed_ms']}ms")
    print("")
    print(f"{'STEP':24} {'STATUS':6} {'MS':>7}  DETAIL")
    print(f"{'-' * 24} {'-' * 6} {'-' * 7}  {'-' * 40}")
    for step in steps:
        detail = step["detail"]
        if len(detail) > 96:
            detail = detail[:93] + "..."
        print(f"{step['name'][:24]:24} {step['status']:6} {step['elapsed_ms']:7d}  {detail}")
    print("")
    print(
        "summary: "
        f"pass={payload['counts']['pass']} "
        f"warn={payload['counts']['warn']} "
        f"fail={payload['counts']['fail']}"
    )
    if payload["ok"]:
        print("result: PASS")
    else:
        print("result: FAIL")
PY
}

export PYTHONDONTWRITEBYTECODE=1
if ! snapshot_mtimes "$MTIME_BEFORE"; then
  echo "e2e-smoke: failed to snapshot watched mtimes" >&2
  exit 1
fi

run_step "render" "required" step_render
run_step "doctor" "doctor" step_doctor
run_step "runtime-render" "required" step_runtime_render
run_step "sync-dry-run" "required" step_sync_dry_run
run_step "compose-config" "required" step_compose_config
run_step "stub-api" "required" step_stub_api
run_step "stub-web" "required" step_stub_web
run_step "cleanup-processes" "required" step_cleanup_processes
run_step "state-mutation" "required" step_state_mutation

summary_rc=0
emit_summary || summary_rc=$?
if [[ "$summary_rc" -ne 0 ]]; then
  exit "$summary_rc"
fi

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
exit 0
