#!/usr/bin/env bash
# Canonical local self-test gate for Skillbox (skillbox-6r53).
#
# This script is the single repo-owned authority that runs every check the
# hosted CI matrix runs, against an isolated checkout of an exact commit SHA,
# using a pinned tool matrix that is provisioned once and cached. It is called
# by .githooks/pre-push and by `make self-test`. GitHub Actions still runs the
# same lanes for untrusted pull requests and for manual recovery
# (workflow_dispatch); trusted-main pushes are gated here instead.
#
# Invariants inherited from the ingredient self-release case study:
#   * protected exact SHA        - the gate always resolves and records a full SHA
#   * isolated source            - lanes run in a throwaway clone, never the worktree
#   * canonical blocking gate    - one command, all lanes, non-zero exit on any failure
#   * build once                 - the pinned toolchain is provisioned once and reused
#   * immutable identity         - pins are literal, never resolved "latest"
#   * behavior plus state proof  - lane exit codes plus a durable receipt
#   * durable redacted receipt   - JSON receipt under the state root, $HOME redacted
#   * explicit recovery          - --refresh re-provisions, --lane re-runs one lane
#   * retention and serialization- receipts pruned to a bound, runs serialized by flock
#   * actual trigger-event proof - the receipt records the trigger that invoked it
#
# Nothing here may weaken the hosted matrix: the pins and the Python version
# list below are contract-tested against .github/workflows/ci.yml.

set -euo pipefail

# --- pinned tool matrix (must match .github/workflows/ci.yml) ------------------
RUFF_VERSION="0.15.20"
SHELLCHECK_PY_VERSION="0.11.0.1"
COVERAGE_VERSION="7.15.0"
PYYAML_VERSION="6.0.3"
CRYPTOGRAPHY_VERSION="49.0.0"
# Test-only imports used by the suite itself (test_sbpd imports jwt; several
# modules import pytest for fixtures/parametrize). Absent from the lane venvs
# these become module-level ImportErrors that read as lane failures.
PYTEST_VERSION="8.4.2"
PYJWT_VERSION="2.13.0"
PYTHON_VERSIONS=("3.11" "3.12" "3.13")
COVERAGE_PYTHON="3.12"
COVERAGE_FAIL_UNDER="80"

RECEIPT_SCHEMA="skillbox.self-test.receipt/1"
RECEIPT_RETENTION="50"

SCRIPT_PATH="${BASH_SOURCE[0]}"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd -P)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"

REV="HEAD"
SOURCE_MODE="git-archive"
REFRESH="0"
EMIT_JSON="0"
TRIGGER="${SKILLBOX_SELF_TEST_TRIGGER:-manual}"
SELECTED_LANES=()

die() {
  printf 'self-test: %s\n' "$*" >&2
  exit 2
}

log() {
  printf '%s\n' "$*" >&2
}

usage() {
  cat <<'USAGE'
Usage: scripts/self-test.sh [options]

Runs the canonical local gate (Ruff, ShellCheck, render, the 3.11/3.12/3.13
unit matrix with 3.12 coverage, and compose config) against an isolated
checkout of an exact commit SHA, then writes a SHA-bound receipt.

Options:
  --rev <rev>        Commit-ish to gate (default: HEAD). Always recorded as a full SHA.
  --worktree         Overlay uncommitted tracked/untracked files onto the checkout.
                     Marks the receipt non-canonical; never used by pre-push.
  --lane <id>        Run only this lane (repeatable). Recovery/debug only; marks
                     the receipt non-canonical. Lane ids: lint shellcheck render
                     contract test-3.11 test-3.12-coverage test-3.13 compose
  --refresh          Re-provision the pinned toolchain even on a cache hit.
  --trigger <name>   Record the invoking trigger in the receipt (default: manual).
  --json             Print the receipt JSON on stdout.
  --print-pins       Print the pinned tool matrix as JSON and exit.
  --print-toolchain-fingerprint
                     Print the toolchain cache fingerprint and exit.
  -h, --help         Show this help.

Environment:
  SKILLBOX_STATE_ROOT                  State root (default: <repo>/.skillbox-state)
  SKILLBOX_SELF_TEST_TOOLCHAIN_DIR     Toolchain cache dir override
  SKILLBOX_SELF_TEST_RECEIPT_DIR       Receipt dir override
  SKILLBOX_SELF_TEST_TRIGGER           Default value for --trigger

Exit codes: 0 all lanes passed, 1 at least one lane failed, 2 gate/usage error.
USAGE
}

print_pins() {
  local versions=""
  local version
  for version in "${PYTHON_VERSIONS[@]}"; do
    if [[ -n "${versions}" ]]; then
      versions="${versions}, "
    fi
    versions="${versions}\"${version}\""
  done
  cat <<PINS
{
  "ruff": "${RUFF_VERSION}",
  "shellcheck_py": "${SHELLCHECK_PY_VERSION}",
  "coverage": "${COVERAGE_VERSION}",
  "pyyaml": "${PYYAML_VERSION}",
  "cryptography": "${CRYPTOGRAPHY_VERSION}",
  "python_versions": [${versions}],
  "coverage_python": "${COVERAGE_PYTHON}",
  "coverage_fail_under": "${COVERAGE_FAIL_UNDER}"
}
PINS
}

fingerprint_material() {
  printf 'ruff=%s;shellcheck_py=%s;coverage=%s;pyyaml=%s;cryptography=%s;pytest=%s;pyjwt=%s;pythons=%s\n' \
    "${RUFF_VERSION}" "${SHELLCHECK_PY_VERSION}" "${COVERAGE_VERSION}" \
    "${PYYAML_VERSION}" "${CRYPTOGRAPHY_VERSION}" "${PYTEST_VERSION}" "${PYJWT_VERSION}" "${PYTHON_VERSIONS[*]}"
}

toolchain_fingerprint() {
  fingerprint_material | sha256sum | cut -d' ' -f1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rev)
      [[ $# -ge 2 ]] || die "--rev needs a value"
      REV="$2"
      shift 2
      ;;
    --worktree)
      SOURCE_MODE="worktree-overlay"
      shift
      ;;
    --lane)
      [[ $# -ge 2 ]] || die "--lane needs a value"
      SELECTED_LANES+=("$2")
      shift 2
      ;;
    --refresh)
      REFRESH="1"
      shift
      ;;
    --trigger)
      [[ $# -ge 2 ]] || die "--trigger needs a value"
      TRIGGER="$2"
      shift 2
      ;;
    --json)
      EMIT_JSON="1"
      shift
      ;;
    --print-pins)
      print_pins
      exit 0
      ;;
    --print-toolchain-fingerprint)
      toolchain_fingerprint
      exit 0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
done

command -v git >/dev/null 2>&1 || die "git is required"
command -v docker >/dev/null 2>&1 || die "docker is required for the compose lane"

# Git hooks export repository-local variables such as GIT_DIR. If those leak
# into the isolated clone below, `git -C "${SRC}" checkout` still targets the
# caller's worktree: it detaches the real checkout and leaves SRC empty. Resolve
# the script-owned root first, then clear exactly Git's documented local vars.
GIT_LOCAL_ENV_VARS="$(git -C "${REPO_ROOT}" rev-parse --local-env-vars 2>/dev/null || true)"
for git_local_env in ${GIT_LOCAL_ENV_VARS}; do
  unset "${git_local_env}"
done
unset GIT_LOCAL_ENV_VARS git_local_env

STATE_ROOT="${SKILLBOX_STATE_ROOT:-${REPO_ROOT}/.skillbox-state}"
TOOLCHAIN_DIR="${SKILLBOX_SELF_TEST_TOOLCHAIN_DIR:-${STATE_ROOT}/self-test/toolchain}"
RECEIPT_DIR="${SKILLBOX_SELF_TEST_RECEIPT_DIR:-${STATE_ROOT}/self-test/receipts}"
mkdir -p "${TOOLCHAIN_DIR}" "${RECEIPT_DIR}"

# --- serialization ------------------------------------------------------------
# Two concurrent gates would race on the shared toolchain cache and could
# publish interleaved receipts. Serialize on the toolchain dir.
LOCK_FILE="${TOOLCHAIN_DIR}/.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"${LOCK_FILE}"
  if ! flock -w 3600 9; then
    die "timed out waiting for another self-test run to finish"
  fi
else
  log "self-test: flock unavailable; concurrent runs are not serialized"
fi

# --- protected exact SHA ------------------------------------------------------
SHA="$(git -C "${REPO_ROOT}" rev-parse --verify "${REV}^{commit}" 2>/dev/null || true)"
[[ -n "${SHA}" ]] || die "cannot resolve '${REV}' to a commit in ${REPO_ROOT}"
TREE_SHA="$(git -C "${REPO_ROOT}" rev-parse --verify "${SHA}^{tree}")"

WORKTREE_CLEAN="true"
if [[ -n "$(git -C "${REPO_ROOT}" status --porcelain 2>/dev/null)" ]]; then
  WORKTREE_CLEAN="false"
fi

CANONICAL="true"
NON_CANONICAL_REASON=""
if [[ "${SOURCE_MODE}" != "git-archive" ]]; then
  CANONICAL="false"
  NON_CANONICAL_REASON="source-mode=${SOURCE_MODE}"
fi
if [[ ${#SELECTED_LANES[@]} -gt 0 ]]; then
  CANONICAL="false"
  if [[ -n "${NON_CANONICAL_REASON}" ]]; then
    NON_CANONICAL_REASON="${NON_CANONICAL_REASON},"
  fi
  NON_CANONICAL_REASON="${NON_CANONICAL_REASON}lane-subset=${SELECTED_LANES[*]}"
fi

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/skillbox-self-test.XXXXXXXX")"

# Grace period between TERM and KILL when sweeping a lane's leftovers.
LANE_REAP_GRACE_SECONDS="${SKILLBOX_SELF_TEST_REAP_GRACE:-5}"
# Process group of the lane currently running, so the EXIT trap can sweep it if
# the gate is interrupted mid-lane. Empty when no lane is in flight.
CURRENT_LANE_PGID=""

# Kill everything still alive in a lane's process group.
#
# By the time this runs the lane's own command has already exited, so any
# surviving member of its group is something the lane spawned and failed to stop
# -- in practice a service a test booted from builds/clients/*/workspace/
# runtime.yaml (fwc serve-mcp, dcg mcp) inside a TemporaryDirectory sandbox.
# Deleting the sandbox removed the files but never signalled those processes:
# they reparented to PID 1 and accumulated as `sh -c` retry loops (~950 leaked
# pairs drove load to ~1267 on 8 vCPUs on 2026-08-07, after a first storm on
# 2026-07-23). A gate run must not outlive itself.
reap_lane_group() {
  local pgid="$1" lane="$2" waited=0
  # Nothing left in the group is the normal, quiet case.
  kill -0 -- "-${pgid}" 2>/dev/null || return 1
  log "self-test: reaping stray processes left behind by lane ${lane}"
  kill -TERM -- "-${pgid}" 2>/dev/null || true
  while kill -0 -- "-${pgid}" 2>/dev/null; do
    if [[ "${waited}" -ge "${LANE_REAP_GRACE_SECONDS}" ]]; then
      kill -KILL -- "-${pgid}" 2>/dev/null || true
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
  return 0
}

cleanup() {
  # Interrupted mid-lane (the gate is killable, and was killed in practice):
  # sweep the in-flight lane before dropping the sandbox, or the same leak
  # happens on every aborted run.
  if [[ -n "${CURRENT_LANE_PGID}" ]]; then
    reap_lane_group "${CURRENT_LANE_PGID}" "interrupted" || true
    CURRENT_LANE_PGID=""
  fi
  rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

SRC="${WORK_DIR}/src"
LOG_DIR="${WORK_DIR}/logs"
mkdir -p "${LOG_DIR}"

# --- isolated source ----------------------------------------------------------
# A --shared clone reads the object store without writing to it, and leaves the
# operator's worktree, index, and refs untouched. The lanes therefore never see
# uncommitted state unless --worktree was requested explicitly.
git clone --quiet --shared --no-checkout "${REPO_ROOT}" "${SRC}"
git -C "${SRC}" checkout --quiet --detach "${SHA}"

if [[ "${SOURCE_MODE}" == "worktree-overlay" ]]; then
  # Copy modified-tracked plus untracked-not-ignored files over the checkout.
  ( cd "${REPO_ROOT}" \
    && git ls-files --modified --others --exclude-standard -z \
    | tar --null --files-from=- -cf - 2>/dev/null ) \
    | tar -xf - -C "${SRC}"
fi

SRC_SHA="$(git -C "${SRC}" rev-parse --verify HEAD)"
[[ "${SRC_SHA}" == "${SHA}" ]] || die "isolated checkout is ${SRC_SHA}, expected ${SHA}"

# --- build once: pinned toolchain --------------------------------------------
FINGERPRINT="$(toolchain_fingerprint)"
STAMP_FILE="${TOOLCHAIN_DIR}/stamp"
TOOL_BIN="${TOOLCHAIN_DIR}/bin"
PY_ROOT="${TOOLCHAIN_DIR}/py"

toolchain_complete() {
  local version
  [[ -f "${STAMP_FILE}" ]] || return 1
  [[ "$(cat "${STAMP_FILE}")" == "${FINGERPRINT}" ]] || return 1
  [[ -x "${TOOL_BIN}/ruff" ]] || return 1
  [[ -x "${TOOL_BIN}/shellcheck" ]] || return 1
  for version in "${PYTHON_VERSIONS[@]}"; do
    [[ -x "${PY_ROOT}/${version}/bin/python" ]] || return 1
  done
  return 0
}

provision_toolchain() {
  local version
  command -v uv >/dev/null 2>&1 \
    || die "uv is required to provision the pinned toolchain (https://docs.astral.sh/uv/); install it, then re-run"

  log "self-test: provisioning pinned toolchain (${FINGERPRINT:0:12})"
  rm -f "${STAMP_FILE}"
  mkdir -p "${TOOL_BIN}" "${PY_ROOT}"

  uv python install "${PYTHON_VERSIONS[@]}" >&2

  for version in "${PYTHON_VERSIONS[@]}"; do
    # --clear so a partial or stale cache is rebuilt instead of erroring out.
    uv venv --quiet --clear --python "${version}" "${PY_ROOT}/${version}" >&2
    uv pip install --quiet --python "${PY_ROOT}/${version}/bin/python" \
      "PyYAML==${PYYAML_VERSION}" \
      "cryptography==${CRYPTOGRAPHY_VERSION}" \
      "coverage==${COVERAGE_VERSION}" \
      "pytest==${PYTEST_VERSION}" \
      "PyJWT==${PYJWT_VERSION}" >&2
    [[ -x "${PY_ROOT}/${version}/bin/python" ]] \
      || die "provisioning produced no interpreter for ${version} at ${PY_ROOT}/${version}/bin/python"
    local actual
    actual="$("${PY_ROOT}/${version}/bin/python" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
    [[ "${actual}" == "${version}" ]] \
      || die "provisioned interpreter for ${version} reports ${actual}"
  done

  uv venv --quiet --clear --python "${COVERAGE_PYTHON}" "${TOOLCHAIN_DIR}/tools" >&2
  uv pip install --quiet --python "${TOOLCHAIN_DIR}/tools/bin/python" \
    "ruff==${RUFF_VERSION}" \
    "shellcheck-py==${SHELLCHECK_PY_VERSION}" >&2
  ln -sf "${TOOLCHAIN_DIR}/tools/bin/ruff" "${TOOL_BIN}/ruff"
  ln -sf "${TOOLCHAIN_DIR}/tools/bin/shellcheck" "${TOOL_BIN}/shellcheck"

  printf '%s\n' "${FINGERPRINT}" >"${STAMP_FILE}"
}

TOOLCHAIN_PROVISIONED="false"
if [[ "${REFRESH}" == "1" ]] || ! toolchain_complete; then
  provision_toolchain
  TOOLCHAIN_PROVISIONED="true"
  toolchain_complete \
    || die "toolchain still incomplete after provisioning; re-run with --refresh"
fi

# Fail closed on a reduced matrix: a missing pinned interpreter must never
# silently drop a lane.
for _version in "${PYTHON_VERSIONS[@]}"; do
  [[ -x "${PY_ROOT}/${_version}/bin/python" ]] \
    || die "pinned interpreter ${_version} missing from ${PY_ROOT}; re-run with --refresh"
done
unset _version

# --- lanes --------------------------------------------------------------------
LANE_TSV="${WORK_DIR}/lanes.tsv"
: >"${LANE_TSV}"
FAILED_LANES=()
RAN_LANES=()

lane_selected() {
  local id="$1"
  local candidate
  if [[ ${#SELECTED_LANES[@]} -eq 0 ]]; then
    return 0
  fi
  for candidate in "${SELECTED_LANES[@]}"; do
    [[ "${candidate}" == "${id}" ]] && return 0
  done
  return 1
}

run_lane() {
  local id="$1"
  shift
  lane_selected "${id}" || return 0

  local started ended elapsed status code reaped lane_pid
  local log_file="${LOG_DIR}/${id}.log"
  started="$(date +%s)"
  set +e
  # `set -m` makes each background job a process-group leader, so the lane and
  # everything it spawns share a group we can signal as a unit afterwards.
  # setsid(1) would be the obvious tool but it does not ship on macOS, and this
  # gate runs on both macOS and Linux; job control is the portable equivalent.
  set -m
  ( cd "${SRC}" && "$@" ) >"${log_file}" 2>&1 &
  lane_pid=$!
  CURRENT_LANE_PGID="${lane_pid}"
  wait "${lane_pid}"
  code="$?"
  set +m
  reaped="false"
  if reap_lane_group "${lane_pid}" "${id}"; then
    reaped="true"
  fi
  CURRENT_LANE_PGID=""
  set -e
  ended="$(date +%s)"
  elapsed="$((ended - started))"

  if [[ "${code}" -eq 0 ]]; then
    status="pass"
    printf 'self-test: PASS %-20s %4ss\n' "${id}" "${elapsed}" >&2
  else
    status="fail"
    FAILED_LANES+=("${id}")
    printf 'self-test: FAIL %-20s %4ss (exit %s)\n' "${id}" "${elapsed}" "${code}" >&2
    log "----- ${id} (last 40 lines) -----"
    tail -n 40 "${log_file}" >&2 || true
    log "----- ${id} failing tests -----"
    # The 40-line tail regularly ends inside a test's stdout (JSON noise) and
    # loses the unittest summary; always surface the failure NAMES too, or a
    # red lane is undiagnosable from the receipt (skillbox-5gth).
    grep -E '^(FAIL|ERROR): ' "${log_file}" | tail -n 30 >&2 || true
    grep -E '^(FAILED|OK)( |$)' "${log_file}" | tail -n 2 >&2 || true
    log "----- end ${id} -----"
  fi

  RAN_LANES+=("${id}")
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
    "${id}" "${status}" "${code}" "${elapsed}" "${reaped}" "$*" >>"${LANE_TSV}"
}

RUN_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RUN_STARTED_EPOCH="$(date +%s)"

log "self-test: gating ${SHA} (mode=${SOURCE_MODE}, trigger=${TRIGGER})"

run_lane "lint" "${TOOL_BIN}/ruff" check .

run_lane "shellcheck" bash -c \
  "\"\$1\" --severity=warning scripts/*.sh install.sh .githooks/pre-commit .githooks/pre-push" \
  _ "${TOOL_BIN}/shellcheck"

run_lane "render" bash -c \
  "\"\$1\" scripts/04-reconcile.py render >\"\$2\"" _ \
  "${PY_ROOT}/${COVERAGE_PYTHON}/bin/python" "${WORK_DIR}/render.json"

# The cross-surface command contract. Read-only: it imports and introspects the
# parsers, the registry, the Makefile and the checked-in baselines, and runs
# nothing -- so it is safe in the isolated checkout and needs no Docker, no
# network, and no operator state. Output is NOT redirected: when this lane
# fails, the drift itself is what the failure tail has to show.
run_lane "contract" "${PY_ROOT}/${COVERAGE_PYTHON}/bin/python" \
  .env-manager/manage.py contract-lint --format json

for version in "${PYTHON_VERSIONS[@]}"; do
  if [[ "${version}" == "${COVERAGE_PYTHON}" ]]; then
    run_lane "test-${version}-coverage" bash -c \
      "\"\$1\" -m coverage erase \
        && \"\$1\" -m coverage run --source=scripts,.env-manager -m unittest discover -s tests \
        && \"\$1\" -m coverage report -m --skip-covered --fail-under=\"\$2\" \
        && \"\$1\" -m coverage xml -o coverage.xml" _ \
      "${PY_ROOT}/${version}/bin/python" "${COVERAGE_FAIL_UNDER}"
  else
    run_lane "test-${version}" \
      "${PY_ROOT}/${version}/bin/python" -m unittest discover -s tests
  fi
done

run_lane "compose" docker compose --env-file .env.example \
  -f docker-compose.yml -f docker-compose.monoserver.yml config -q

RUN_FINISHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RUN_DURATION="$(($(date +%s) - RUN_STARTED_EPOCH))"

if [[ ${#RAN_LANES[@]} -eq 0 ]]; then
  die "no lanes matched --lane ${SELECTED_LANES[*]:-}"
fi

GATE_STATUS="pass"
if [[ ${#FAILED_LANES[@]} -gt 0 ]]; then
  GATE_STATUS="fail"
fi

# --- durable redacted receipt -------------------------------------------------
RECEIPT_FILE="${RECEIPT_DIR}/${SHA}-$(date -u +%Y%m%dT%H%M%SZ).json"

SELF_TEST_RECEIPT_ENV=(
  "ST_SCHEMA=${RECEIPT_SCHEMA}"
  "ST_COMMIT=${SHA}"
  "ST_TREE=${TREE_SHA}"
  "ST_SOURCE_MODE=${SOURCE_MODE}"
  "ST_WORKTREE_CLEAN=${WORKTREE_CLEAN}"
  "ST_CANONICAL=${CANONICAL}"
  "ST_NON_CANONICAL_REASON=${NON_CANONICAL_REASON}"
  "ST_TRIGGER=${TRIGGER}"
  "ST_STATUS=${GATE_STATUS}"
  "ST_STARTED_AT=${RUN_STARTED_AT}"
  "ST_FINISHED_AT=${RUN_FINISHED_AT}"
  "ST_DURATION=${RUN_DURATION}"
  "ST_FINGERPRINT=${FINGERPRINT}"
  "ST_PROVISIONED=${TOOLCHAIN_PROVISIONED}"
  "ST_LANE_TSV=${LANE_TSV}"
  "ST_RECEIPT_FILE=${RECEIPT_FILE}"
  "ST_RECEIPT_DIR=${RECEIPT_DIR}"
  "ST_RETENTION=${RECEIPT_RETENTION}"
  "ST_PINS=$(print_pins)"
  "ST_HOME=${HOME:-}"
)

env "${SELF_TEST_RECEIPT_ENV[@]}" python3 - <<'PY'
import json
import os
import platform

home = os.environ.get("ST_HOME") or ""


def redact(value: str) -> str:
    if home and len(home) > 1:
        value = value.replace(home, "~")
    return value


lanes = []
with open(os.environ["ST_LANE_TSV"], encoding="utf-8") as handle:
    for line in handle:
        line = line.rstrip("\n")
        if not line:
            continue
        lane_id, status, code, seconds, reaped, command = line.split("\t", 5)
        lanes.append(
            {
                "id": lane_id,
                "status": status,
                "exit_code": int(code),
                "duration_s": int(seconds),
                # True when the lane left processes running and the gate had to
                # sweep its process group. A durable signal that some suite is
                # starting services it never stops.
                "reaped_stray_processes": reaped == "true",
                "command": redact(command),
            }
        )

receipt = {
    "schema": os.environ["ST_SCHEMA"],
    "gate": "scripts/self-test.sh",
    "repo": "skillbox",
    "commit": os.environ["ST_COMMIT"],
    "tree": os.environ["ST_TREE"],
    "source_mode": os.environ["ST_SOURCE_MODE"],
    "worktree_clean": os.environ["ST_WORKTREE_CLEAN"] == "true",
    "canonical": os.environ["ST_CANONICAL"] == "true",
    "non_canonical_reason": os.environ["ST_NON_CANONICAL_REASON"] or None,
    "trigger": os.environ["ST_TRIGGER"],
    "status": os.environ["ST_STATUS"],
    "started_at": os.environ["ST_STARTED_AT"],
    "finished_at": os.environ["ST_FINISHED_AT"],
    "duration_s": int(os.environ["ST_DURATION"]),
    "toolchain": {
        "fingerprint": os.environ["ST_FINGERPRINT"],
        "provisioned_this_run": os.environ["ST_PROVISIONED"] == "true",
        "pins": json.loads(os.environ["ST_PINS"]),
    },
    "host": {"system": platform.system(), "machine": platform.machine()},
    "lanes": lanes,
    "failed_lanes": [lane["id"] for lane in lanes if lane["status"] != "pass"],
}

receipt_file = os.environ["ST_RECEIPT_FILE"]
with open(receipt_file, "w", encoding="utf-8") as handle:
    json.dump(receipt, handle, indent=2, sort_keys=True)
    handle.write("\n")

receipt_dir = os.environ["ST_RECEIPT_DIR"]
latest = os.path.join(receipt_dir, "latest.json")
with open(latest, "w", encoding="utf-8") as handle:
    json.dump(receipt, handle, indent=2, sort_keys=True)
    handle.write("\n")

# Retention: keep the newest N SHA-bound receipts (by mtime, not by SHA name).
retention = int(os.environ["ST_RETENTION"])
entries = [
    os.path.join(receipt_dir, name)
    for name in os.listdir(receipt_dir)
    if name.endswith(".json") and name != "latest.json"
]
entries.sort(key=os.path.getmtime)
for stale in entries[:-retention] if len(entries) > retention else []:
    os.remove(stale)
PY

log "self-test: receipt $(printf '%s' "${RECEIPT_FILE}" | sed "s#^${HOME:-/nonexistent}#~#")"

if [[ "${EMIT_JSON}" == "1" ]]; then
  cat "${RECEIPT_FILE}"
fi

if [[ "${GATE_STATUS}" != "pass" ]]; then
  log "self-test: FAILED lanes: ${FAILED_LANES[*]}"
  log "self-test: recovery: re-run one lane with 'scripts/self-test.sh --lane <id>',"
  log "self-test:           rebuild the pinned toolchain with 'scripts/self-test.sh --refresh'."
  exit 1
fi

if [[ "${CANONICAL}" == "true" ]]; then
  log "self-test: PASS (canonical) ${SHA}"
else
  log "self-test: PASS (non-canonical: ${NON_CANONICAL_REASON}) ${SHA}"
fi
