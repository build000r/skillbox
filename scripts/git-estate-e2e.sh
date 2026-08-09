#!/usr/bin/env bash
#
# End-to-end exercise of the REAL `sbp git` front door against a temp fixture
# estate (never ~/repos). Covers: `sbp git`, `sbp git --json`, `sbp gs`,
# `sbp git status`, `--only dirty`, `--only unregistered`, and the
# `sbp git push` refusal. Read-only by design: the fixture estate lives in a
# mktemp dir, SKILLBOX_CONFIG_ROOT points at a temp stand-in registry, and the
# scan itself never fetches or mutates.
#
# Every phase and assertion is logged ('PHASE: ...' / 'ASSERT: ...') so a CI
# failure is diagnosable from the log alone. Exits nonzero on any failure.
#
# Usage: scripts/git-estate-e2e.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SBP="${ROOT_DIR}/scripts/sbp"
PYTHON_BIN="${PYTHON:-python3}"

log() { printf '%s\n' "$*"; }
fail() {
  log "FAIL: $*"
  exit 1
}
assert_ok() { log "ASSERT: $*"; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/git-estate-e2e.XXXXXX")"
# Canonicalize (macOS TMPDIR carries a trailing slash; scan rows are
# normalized paths, so assertions must compare like with like).
WORK="$(cd "${WORK}" && pwd -P)"
cleanup() { rm -rf "${WORK}"; }
trap cleanup EXIT

ESTATE="${WORK}/estate"
ORIGINS="${WORK}/origins"
CONFIG="${WORK}/config"
mkdir -p "${ESTATE}" "${ORIGINS}" "${CONFIG}/scripts" "${CONFIG}/registry"

# ---------------------------------------------------------------------------
# PHASE: fixture setup — hermetic git config + deterministic estate
# ---------------------------------------------------------------------------
log "PHASE: fixture setup under ${WORK}"

export GIT_CONFIG_NOSYSTEM=1
export GIT_CONFIG_GLOBAL="${WORK}/gitconfig"
export GIT_TERMINAL_PROMPT=0
export GIT_OPTIONAL_LOCKS=0
cat > "${GIT_CONFIG_GLOBAL}" <<'EOF'
[user]
	email = fixture@example.invalid
	name = Git Estate E2E Fixture
[init]
	defaultBranch = main
[commit]
	gpgsign = false
EOF

make_repo() { # make_repo <path>
  local repo="$1"
  mkdir -p "${repo}"
  git -C "${repo}" init -q -b main
  printf 'base\n' > "${repo}/tracked.txt"
  git -C "${repo}" add tracked.txt
  git -C "${repo}" commit -q -m base
}

make_clone() { # make_clone <name> -> clone at $ESTATE/<name>
  local name="$1"
  make_repo "${ORIGINS}/${name}-origin"
  git clone -q "file://${ORIGINS}/${name}-origin" "${ESTATE}/${name}"
}

# a-clean: pristine clone (registered).
make_clone "a-clean"

# b-dirty: staged + unstaged + untracked (registered).
make_clone "b-dirty"
printf 'staged\n' > "${ESTATE}/b-dirty/staged.txt"
git -C "${ESTATE}/b-dirty" add staged.txt
printf 'modified\n' > "${ESTATE}/b-dirty/tracked.txt"
printf 'loose\n' > "${ESTATE}/b-dirty/loose.txt"

# c-ahead: one local commit ahead of upstream (registered).
make_clone "c-ahead"
printf 'local\n' > "${ESTATE}/c-ahead/local.txt"
git -C "${ESTATE}/c-ahead" add local.txt
git -C "${ESTATE}/c-ahead" commit -q -m "local work"

# e-midop: merge conflict in flight (registered).
make_repo "${ESTATE}/e-midop"
git -C "${ESTATE}/e-midop" checkout -q -b feature
printf 'feature\n' > "${ESTATE}/e-midop/tracked.txt"
git -C "${ESTATE}/e-midop" commit -q -am "feature change"
git -C "${ESTATE}/e-midop" checkout -q main
printf 'mainline\n' > "${ESTATE}/e-midop/tracked.txt"
git -C "${ESTATE}/e-midop" commit -q -am "main change"
if git -C "${ESTATE}/e-midop" merge feature >/dev/null 2>&1; then
  fail "fixture merge should conflict"
fi

# g-unregistered: scanned repo absent from the registry.
make_repo "${ESTATE}/g-unregistered"

# z-ignored: matched by a registry ignore rule (must never be a row).
make_repo "${ESTATE}/z-ignored"

# Temp config root: registry_doctor.py stand-in (same three entry points the
# real skillbox-config helper exposes) + a JSON-bodied repos.yaml (JSON is
# valid YAML) with a stale entry whose checkout never exists on disk.
cat > "${CONFIG}/scripts/registry_doctor.py" <<'EOF'
import fnmatch
import json
import os
from pathlib import Path


def normalize_path(value):
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    return os.path.abspath(os.path.normpath(expanded))


def load_registry(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def normalize_registry(payload, root_overrides):
    repos = []
    for item in payload.get("repos") or []:
        item = dict(item)
        item["path"] = normalize_path(item["path"])
        repos.append(item)
    ignore = []
    for item in payload.get("ignore") or []:
        item = dict(item)
        if "path" in item:
            item["path"] = normalize_path(item["path"])
        if "pattern" in item:
            item["pattern"] = normalize_path(item["pattern"])
        ignore.append(item)
    return {
        "roots": [],
        "max_depth": None,
        "prune_dir_names": set(),
        "repos": repos,
        "ignore": ignore,
    }


def _is_same_or_child(path, parent):
    try:
        Path(path).relative_to(parent)
        return True
    except ValueError:
        return False


def matching_ignore(path, ignore_rules):
    for rule in ignore_rules:
        if rule.get("path") and _is_same_or_child(path, rule["path"]):
            return rule
        if rule.get("pattern") and fnmatch.fnmatch(path, rule["pattern"]):
            return rule
    return None
EOF

"${PYTHON_BIN}" - "$ESTATE" "$CONFIG" <<'EOF'
import json
import sys

estate, config = sys.argv[1], sys.argv[2]
payload = {
    "repos": [
        {"id": "a-clean", "path": f"{estate}/a-clean"},
        {"id": "b-dirty", "path": f"{estate}/b-dirty"},
        {"id": "c-ahead", "path": f"{estate}/c-ahead"},
        {"id": "e-midop", "path": f"{estate}/e-midop"},
        {"id": "gone", "path": f"{estate}/gone-checkout"},
    ],
    "ignore": [{"path": f"{estate}/z-ignored", "reason": "e2e fixture"}],
}
with open(f"{config}/registry/repos.yaml", "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
EOF
assert_ok "fixture estate built (5 scannable repos, 1 ignored, 1 stale entry)"

run_sbp() {
  env \
    SKILLBOX_ROOT="${ROOT_DIR}" \
    SKILLBOX_INVOKE_CWD="${ESTATE}" \
    SKILLBOX_CONFIG_ROOT="${CONFIG}" \
    "${SBP}" "$@" < /dev/null
}

SCAN_ARGS=(--root "${ESTATE}" --depth 2)

json_assert() { # json_assert <json-file> <description> <python-expr over payload>
  local file="$1" description="$2" expr="$3"
  if ! "${PYTHON_BIN}" - "${file}" "${expr}" <<'EOF'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
if not eval(sys.argv[2], {"payload": payload}):
    sys.exit(1)
EOF
  then
    log "---- payload under test (${file}) ----"
    cat "${file}"
    fail "${description}"
  fi
  assert_ok "${description}"
}

# ---------------------------------------------------------------------------
# PHASE: sbp git (text view)
# ---------------------------------------------------------------------------
log "PHASE: sbp git (text view)"
TEXT_OUT="${WORK}/git.txt"
run_sbp git "${SCAN_ARGS[@]}" > "${TEXT_OUT}"
grep -q "estate: 5 repos under ${ESTATE}" "${TEXT_OUT}" \
  || { cat "${TEXT_OUT}"; fail "text view must report 5 scanned repos"; }
assert_ok "text view reports 5 scanned repos"
grep -q "1 ignored by registry rules" "${TEXT_OUT}" \
  || { cat "${TEXT_OUT}"; fail "text view must report the ignore-rule hit"; }
assert_ok "ignore-rule hit is reported"
grep -q "${ESTATE}/b-dirty" "${TEXT_OUT}" \
  || { cat "${TEXT_OUT}"; fail "dirty repo row missing from text view"; }
assert_ok "dirty repo row present"
grep -q "${ESTATE}/g-unregistered  \[unregistered\]" "${TEXT_OUT}" \
  || { cat "${TEXT_OUT}"; fail "unregistered marker missing"; }
assert_ok "unregistered marker present"
grep -q "stale-registered: 1 registry entries" "${TEXT_OUT}" \
  || { cat "${TEXT_OUT}"; fail "stale-registered section missing"; }
assert_ok "stale-registered section present"
if grep -q $'\033\[' "${TEXT_OUT}"; then
  cat -v "${TEXT_OUT}"
  fail "piped output must be plain (no ANSI escapes)"
fi
assert_ok "piped output carries no ANSI escapes"
if grep -q "${ESTATE}/z-ignored" "${TEXT_OUT}"; then
  cat "${TEXT_OUT}"
  fail "ignored repo must never appear as a row"
fi
assert_ok "ignored repo never appears"

# ---------------------------------------------------------------------------
# PHASE: sbp git --json (sbp-git/v1 envelope)
# ---------------------------------------------------------------------------
log "PHASE: sbp git --json"
JSON_OUT="${WORK}/git.json"
run_sbp git --json "${SCAN_ARGS[@]}" > "${JSON_OUT}"
json_assert "${JSON_OUT}" "envelope schema is sbp-git/v1" \
  'payload["schema"] == "sbp-git/v1"'
json_assert "${JSON_OUT}" "repo_count is 5" 'payload["repo_count"] == 5'
json_assert "${JSON_OUT}" "ignored_count is 1" 'payload["ignored_count"] == 1'
json_assert "${JSON_OUT}" "registry join applied" 'payload["registry_applied"] is True'
json_assert "${JSON_OUT}" "registration summary counts 4 registered / 1 unregistered / 1 stale" \
  'payload["registration_summary"] == {"registered": 4, "unregistered": 1, "unknown": 0, "stale_registered": 1}'
json_assert "${JSON_OUT}" "stale entry names the gone checkout" \
  '[e["id"] for e in payload["stale_registered"]] == ["gone"]'
json_assert "${JSON_OUT}" "rows are risk-sorted (mid-op first, clean last)" \
  '[r["risk_band"] for r in payload["repos"]] == ["mid-op", "dirty", "ahead", "no-remote", "clean"]'

# ---------------------------------------------------------------------------
# PHASE: sbp gs / sbp git status aliases
# ---------------------------------------------------------------------------
log "PHASE: sbp gs and sbp git status aliases"
GS_OUT="${WORK}/gs.json"
STATUS_OUT="${WORK}/git-status.json"
run_sbp gs --json "${SCAN_ARGS[@]}" > "${GS_OUT}"
run_sbp git status --json "${SCAN_ARGS[@]}" > "${STATUS_OUT}"
if ! "${PYTHON_BIN}" - "${JSON_OUT}" "${GS_OUT}" "${STATUS_OUT}" <<'EOF'
import json
import sys

def rows(path):
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return [(row["path"], row["risk_band"]) for row in payload["repos"]]

base = rows(sys.argv[1])
for alias in sys.argv[2:]:
    if rows(alias) != base:
        sys.exit(1)
EOF
then
  fail "gs / git status aliases must return the same rows as git"
fi
assert_ok "gs and git status return identical rows to git"

# ---------------------------------------------------------------------------
# PHASE: sbp git --only dirty
# ---------------------------------------------------------------------------
log "PHASE: sbp git --only dirty"
DIRTY_OUT="${WORK}/only-dirty.json"
run_sbp git --json --only dirty "${SCAN_ARGS[@]}" > "${DIRTY_OUT}"
json_assert "${DIRTY_OUT}" "--only dirty keeps exactly the dirty rows (mid-op conflict included)" \
  'sorted(r["path"].rsplit("/", 1)[-1] for r in payload["repos"]) == ["b-dirty", "e-midop"]'
json_assert "${DIRTY_OUT}" "--only dirty rows all carry the dirty class" \
  'all("dirty" in r["classes"] for r in payload["repos"])'
json_assert "${DIRTY_OUT}" "--only dirty echoes its filter" 'payload["filters"] == ["dirty"]'

# ---------------------------------------------------------------------------
# PHASE: sbp git --only unregistered
# ---------------------------------------------------------------------------
log "PHASE: sbp git --only unregistered"
UNREG_OUT="${WORK}/only-unregistered.json"
run_sbp git --json --only unregistered "${SCAN_ARGS[@]}" > "${UNREG_OUT}"
json_assert "${UNREG_OUT}" "--only unregistered keeps exactly g-unregistered" \
  '[r["path"].rsplit("/", 1)[-1] for r in payload["repos"]] == ["g-unregistered"]'
json_assert "${UNREG_OUT}" "the row carries registration=unregistered" \
  'payload["repos"][0]["registration"] == "unregistered"'
json_assert "${UNREG_OUT}" "the row carries the registry handoff fix" \
  'any("register in" in fix for fix in payload["repos"][0]["fix"])'

# ---------------------------------------------------------------------------
# PHASE: sbp git push refusal (viewer must never proxy mutating git)
# ---------------------------------------------------------------------------
log "PHASE: sbp git push refusal"
PUSH_ERR="${WORK}/push.err"
set +e
run_sbp git push > /dev/null 2> "${PUSH_ERR}"
PUSH_RC=$?
set -e
[[ ${PUSH_RC} -eq 2 ]] \
  || { cat "${PUSH_ERR}"; fail "sbp git push must exit 2 (got ${PUSH_RC})"; }
assert_ok "sbp git push exits 2"
grep -q "refusing to proxy" "${PUSH_ERR}" \
  || { cat "${PUSH_ERR}"; fail "refusal message missing from stderr"; }
assert_ok "refusal message names the proxy refusal"
grep -q "Usage:" "${PUSH_ERR}" \
  || { cat "${PUSH_ERR}"; fail "refusal must include usage"; }
assert_ok "refusal includes usage"

log "PASS: git-estate e2e complete (7 phases green)"
