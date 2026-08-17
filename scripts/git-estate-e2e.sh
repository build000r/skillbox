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

# h-external: ahead of an EXTERNAL upstream (registered as external). Cloned
# from a local origin so the ahead count is real and offline, then repointed at
# a github URL owned by somebody else. Nothing fetches: ahead/behind read the
# local remote-tracking ref, which survives the URL swap. Without the ownership
# join this row's advice is `git push` — straight at a repo the operator does
# not own, which is what the 2026-08-15 live run would have been told 3 times.
make_clone "h-external"
printf 'external\n' > "${ESTATE}/h-external/local.txt"
git -C "${ESTATE}/h-external" add local.txt
git -C "${ESTATE}/h-external" commit -q -m "work on somebody else's repo"
git -C "${ESTATE}/h-external" remote set-url origin \
  "https://github.com/tetsuo-ai/agenc-core.git"

# i-misconfigured: the cfo-qbo-control-plane shape (live evidence 2026-08-15).
# Its branch tracks origin/main while its commits are already on
# origin/codex/qbo at identical SHA, so git itself reports a divergence that
# does not exist. Without the mismatch probe this row sits at the TOP of the
# risk table with a reconcile handoff for nothing.
make_clone "i-misconfigured"
git -C "${ESTATE}/i-misconfigured" checkout -q -b codex/qbo
printf 'w1\n' > "${ESTATE}/i-misconfigured/w1.txt"
git -C "${ESTATE}/i-misconfigured" add w1.txt
git -C "${ESTATE}/i-misconfigured" commit -q -m "published work 1"
printf 'w2\n' > "${ESTATE}/i-misconfigured/w2.txt"
git -C "${ESTATE}/i-misconfigured" add w2.txt
git -C "${ESTATE}/i-misconfigured" commit -q -m "published work 2"
# Published at identical SHA (a branch the origin does not have checked out)...
git -C "${ESTATE}/i-misconfigured" push -q origin codex/qbo
# ...but pointed at the wrong ref.
git -C "${ESTATE}/i-misconfigured" branch -q --set-upstream-to=origin/main codex/qbo
# Advance origin/main so the row also looks "behind", completing the shape.
printf 'moved on\n' > "${ORIGINS}/i-misconfigured-origin/mainline.txt"
git -C "${ORIGINS}/i-misconfigured-origin" add mainline.txt
git -C "${ORIGINS}/i-misconfigured-origin" commit -q -m "origin main moves on"
git -C "${ESTATE}/i-misconfigured" fetch -q origin

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
    # metadata.owner is the operator account ownership derivation compares
    # remote URLs against; the estate model declares it, Python does not.
    "metadata": {"owner": "build000r"},
    "repos": [
        {"id": "a-clean", "path": f"{estate}/a-clean"},
        {"id": "b-dirty", "path": f"{estate}/b-dirty"},
        {"id": "c-ahead", "path": f"{estate}/c-ahead"},
        {"id": "e-midop", "path": f"{estate}/e-midop"},
        {"id": "i-misconfigured", "path": f"{estate}/i-misconfigured"},
        {
            "id": "h-external",
            "path": f"{estate}/h-external",
            "ownership": "external-upstream",
            "note": "no-push fixture: upstream belongs to another account",
        },
        {"id": "gone", "path": f"{estate}/gone-checkout"},
        {
            "id": "sand",
            "path": f"{estate}/sand",
            "located": "d3c",
            "note": "important on d3c",
        },
    ],
    "ignore": [{"path": f"{estate}/z-ignored", "reason": "e2e fixture"}],
}
with open(f"{config}/registry/repos.yaml", "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
EOF
assert_ok "fixture estate built (7 scannable repos, 1 ignored, 2 stale entries)"

run_sbp() {
  # Receipts store and amp guard scripts are pinned at nonexistent paths:
  # absent stores/scripts add NOTHING, keeping the run hermetic on hosts
  # that carry a real reconcile state dir or skills-private checkout.
  env \
    SKILLBOX_ROOT="${ROOT_DIR}" \
    SKILLBOX_INVOKE_CWD="${ESTATE}" \
    SKILLBOX_CONFIG_ROOT="${CONFIG}" \
    SKILLBOX_RECONCILE_RECEIPTS_DIR="${WORK}/no-receipts" \
    SKILLBOX_AMP_CAPSULE_GUARD="${WORK}/no-capsule-guard" \
    SKILLBOX_AMP_CAMPAIGN_GUARD="${WORK}/no-campaign-guard" \
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
grep -q "estate: 7 repos under ${ESTATE}" "${TEXT_OUT}" \
  || { cat "${TEXT_OUT}"; fail "text view must report 7 scanned repos"; }
assert_ok "text view reports 7 scanned repos"
grep -q "1 ignored by registry rules" "${TEXT_OUT}" \
  || { cat "${TEXT_OUT}"; fail "text view must report the ignore-rule hit"; }
assert_ok "ignore-rule hit is reported"
grep -q "${ESTATE}/b-dirty" "${TEXT_OUT}" \
  || { cat "${TEXT_OUT}"; fail "dirty repo row missing from text view"; }
assert_ok "dirty repo row present"
grep -q "${ESTATE}/g-unregistered  \[unregistered\]" "${TEXT_OUT}" \
  || { cat "${TEXT_OUT}"; fail "unregistered marker missing"; }
assert_ok "unregistered marker present"
grep -q "stale-registered: 2 registry entries with no repo on disk (1 located elsewhere, 1 unaccounted)" "${TEXT_OUT}" \
  || { cat "${TEXT_OUT}"; fail "stale-registered section (with located breakdown) missing"; }
assert_ok "stale-registered section present with located breakdown"
grep -q "${ESTATE}/sand  \[located: d3c\]  -> lives on d3c" "${TEXT_OUT}" \
  || { cat "${TEXT_OUT}"; fail "located stale entry must render verify-there advice"; }
assert_ok "located stale entry renders verify-there advice"
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
json_assert "${JSON_OUT}" "repo_count is 7" 'payload["repo_count"] == 7'
json_assert "${JSON_OUT}" "ignored_count is 1" 'payload["ignored_count"] == 1'
json_assert "${JSON_OUT}" "registry join applied" 'payload["registry_applied"] is True'
json_assert "${JSON_OUT}" "registration summary counts 6 registered / 1 unregistered / 2 stale" \
  'payload["registration_summary"] == {"registered": 6, "unregistered": 1, "unknown": 0, "stale_registered": 2}'
json_assert "${JSON_OUT}" "stale entries name the gone checkout and sand" \
  '[e["id"] for e in payload["stale_registered"]] == ["gone", "sand"]'
json_assert "${JSON_OUT}" "located stale entry carries located/note and verify-there fix" \
  'payload["stale_registered"][1]["located"] == "d3c" and payload["stale_registered"][1]["note"] == "important on d3c" and payload["stale_registered"][1]["fix"][0].startswith("lives on d3c")'
json_assert "${JSON_OUT}" "unannotated stale entry keeps remove-or-repoint and no located field" \
  '"located" not in payload["stale_registered"][0] and payload["stale_registered"][0]["fix"][0].startswith("remove or repoint")'
json_assert "${JSON_OUT}" "amp guard absent adds nothing" '"amp" not in payload'
json_assert "${JSON_OUT}" "rows are risk-sorted (mid-op first, clean last)" \
  '[r["risk_band"] for r in payload["repos"]] == ["mid-op", "dirty", "ahead", "ahead", "no-remote", "clean", "clean"]'

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
# PHASE: lane plan (the envelope hands over the division)
# ---------------------------------------------------------------------------
log "PHASE: lane plan"
json_assert "${JSON_OUT}" "the envelope carries a lane plan" \
  'isinstance(payload.get("lanes"), list) and len(payload["lanes"]) > 0'
json_assert "${JSON_OUT}" "every lane carries the full contract" \
  'all(set(("id","kind","repos","write_scope","rationale","suggested_concurrency")) <= set(l) for l in payload["lanes"])'
json_assert "${JSON_OUT}" "lane ids are sequential" \
  '[l["id"] for l in payload["lanes"]] == ["L%d" % (i+1) for i in range(len(payload["lanes"]))]'
json_assert "${JSON_OUT}" "no lane emits an undeclared kind" \
  'all(l["kind"] in ("withheld","diverged","dirty-behind","converge","push-ahead","unregistered-dirty","small-dirty","mechanical-cluster") for l in payload["lanes"])'
json_assert "${JSON_OUT}" "no lane ends in a side ref" \
  'not any(w in l["kind"] for l in payload["lanes"] for w in ("safety","backup","snapshot"))'
json_assert "${JSON_OUT}" "every issue row is placed exactly once" \
  'sorted(p for l in payload["lanes"] for p in l["repos"]) == sorted(set(p for l in payload["lanes"] for p in l["repos"]))'
json_assert "${JSON_OUT}" "the mid-op row is withheld as a judgment block" \
  'any(l["kind"] == "withheld" and any(w["path"].endswith("/e-midop") for w in l.get("withheld") or []) for l in payload["lanes"])'
json_assert "${JSON_OUT}" "a withheld lane is not dispatchable work" \
  'all(l["suggested_concurrency"] == 0 for l in payload["lanes"] if l["kind"] == "withheld")'
json_assert "${JSON_OUT}" "the external-upstream row is never dispatched to push" \
  'not any(l["kind"] == "push-ahead" and any(p.endswith("/h-external") for p in l["repos"]) for l in payload["lanes"])'
json_assert "${JSON_OUT}" "write_scope is a superset of repos in every lane" \
  'all(set(l["repos"]) <= set(l["write_scope"]) for l in payload["lanes"])'
grep -qE "^lanes: [0-9]+ " "${TEXT_OUT}" \
  && assert_ok "tty carries one lane summary line" \
  || fail "tty lane line missing"
test "$(grep -c '^lanes:' "${TEXT_OUT}")" -eq 1 \
  && assert_ok "tty gained exactly one line, no table bloat" \
  || fail "tty lane output is more than one line"

# ---------------------------------------------------------------------------
# PHASE: misconfigured-upstream detection (the false-diverged class)
# ---------------------------------------------------------------------------
log "PHASE: misconfigured upstream"
MIS='next(r for r in payload["repos"] if r["path"].endswith("/i-misconfigured"))'
json_assert "${JSON_OUT}" "the misconfigured row is detected" \
  "${MIS}[\"upstream_mismatch\"] is not None"
json_assert "${JSON_OUT}" "it names the configured ref and the same-name ref" \
  "${MIS}[\"upstream_mismatch\"][\"configured\"] == \"origin/main\" and ${MIS}[\"upstream_mismatch\"][\"same_name\"] == \"origin/codex/qbo\""
json_assert "${JSON_OUT}" "the same-name ref explains every local commit" \
  "${MIS}[\"upstream_mismatch\"][\"ahead_vs_same_name\"] == 0"
json_assert "${JSON_OUT}" "git itself still reported a divergence (the premise)" \
  "${MIS}[\"ahead\"] > 0 and ${MIS}[\"behind\"] > 0"
json_assert "${JSON_OUT}" "but the row does NOT band diverged" \
  "${MIS}[\"risk_band\"] != \"diverged\""
json_assert "${JSON_OUT}" "the fix repairs the upstream" \
  "any(\"--set-upstream-to origin/codex/qbo\" in f for f in ${MIS}[\"fix\"])"
json_assert "${JSON_OUT}" "the fix is NOT a reconcile handoff" \
  "not any(\"do not hand-merge\" in f for f in ${MIS}[\"fix\"])"
json_assert "${JSON_OUT}" "no OTHER row was reclassified" \
  'sum(1 for r in payload["repos"] if r["upstream_mismatch"]) == 1'
grep -q "\[upstream-misconfigured\]" "${TEXT_OUT}" \
  && assert_ok "tty marks the misconfigured row" \
  || fail "tty did not mark the misconfigured row"

# ---------------------------------------------------------------------------
# PHASE: ownership + push policy join (the no-push contract)
# ---------------------------------------------------------------------------
log "PHASE: ownership + push policy"
json_assert "${JSON_OUT}" "every row carries the ownership join" \
  'all(set(("ownership", "ownership_source", "push_policy", "push_policy_reason")) <= set(r) for r in payload["repos"])'
json_assert "${JSON_OUT}" "the external row is external-upstream from the registry" \
  'next(r for r in payload["repos"] if r["path"].endswith("/h-external"))["ownership"] == "external-upstream"'
json_assert "${JSON_OUT}" "the external row resolves via the registry, not a guess" \
  'next(r for r in payload["repos"] if r["path"].endswith("/h-external"))["ownership_source"] == "registry"'
json_assert "${JSON_OUT}" "the external row is no-push" \
  'next(r for r in payload["repos"] if r["path"].endswith("/h-external"))["push_policy"] == "no-push"'
json_assert "${JSON_OUT}" "the external row is genuinely ahead (the advice mattered)" \
  'next(r for r in payload["repos"] if r["path"].endswith("/h-external"))["ahead"] > 0'
json_assert "${JSON_OUT}" "NO row is ever advised to push against a no-push policy" \
  'not any(any("git push" in f or f.endswith(" push") for f in r["fix"]) for r in payload["repos"] if r["push_policy"] != "push")'
json_assert "${JSON_OUT}" "the external row still shows how to inspect the commits" \
  'any("log --oneline" in f for f in next(r for r in payload["repos"] if r["path"].endswith("/h-external"))["fix"])'
json_assert "${JSON_OUT}" "the engine captured remote URLs (the schema-additive probe)" \
  'any(r.get("remotes") for r in payload["repos"])'
grep -q "\[no-push\]" "${TEXT_OUT}" \
  && assert_ok "tty marks the no-push row" \
  || fail "tty did not mark the no-push row"
grep -qv "PUSH_POLICY" "${TEXT_OUT}" \
  && assert_ok "tty gained no push-policy column" \
  || fail "tty grew a column"

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
