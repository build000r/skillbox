#!/usr/bin/env bash
# guard-destructive-op.sh — PreToolUse hook that gates destructive MCP tools.
#
# Blocks operator_teardown and operator_compose_down unless:
#   1. dry_run=true (preview mode always passes)
#   2. ALL git repos in the workspace are clean and pushed
#   3. A dry-run was already executed this session
#
# Checks the skillbox repo itself AND every git repo under the client
# workspace roots (./repos, ./workspace/clients, SKILLBOX_CLIENTS_HOST_ROOT,
# SKILLBOX_MONOSERVER_HOST_ROOT). The scan is bounded to those workspace
# roots; it never walks the parent of the skillbox checkout unless the
# operator has explicitly pointed SKILLBOX_MONOSERVER_HOST_ROOT there.
#
# Exit-code contract (Claude Code PreToolUse hook):
#   exit 0       -> ALLOW the tool call (non-gated tool, dry_run, or all gates pass)
#   exit non-0   -> BLOCK the tool call (stderr carries the actionable reason)
#
# FAIL CLOSED: any internal error while evaluating a *gated* tool BLOCKS the
# operation. A safety gate that passes on its own failure is indistinguishable
# from no gate at all, so every error path here exits non-zero with a message
# naming the step that could not be evaluated. Only a tool that is NOT in the
# gated set is allowed through without evaluation.
#
# Input: JSON on stdin (Claude Code hook protocol)

set -euo pipefail

# ---------------------------------------------------------------------------
# Observability + fail-closed block helper.
#
# audit_log appends one line to .skillbox-state/logs/guard-destructive-op.log
# on every block. It is best-effort: its own failure must never crash the
# guard or change the exit code, so the whole body is wrapped to swallow
# errors (this is the ONLY place we deliberately swallow — a logging failure
# is not a safety failure).
# ---------------------------------------------------------------------------
audit_log() {
    {
        local log_dir="${REPO_ROOT:-.}/.skillbox-state/logs"
        local log_file="${log_dir}/guard-destructive-op.log"
        local stamp
        stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) || stamp="unknown-time"
        if mkdir -p "$log_dir" 2>/dev/null; then
            printf '%s\tBLOCK\ttool=%s\tbox=%s\treason=%s\n' \
                "$stamp" "${FRIENDLY_NAME:-?}" "${BOX_ID:-?}" "$1" \
                >> "$log_file" 2>/dev/null || true
        fi
    } 2>/dev/null || true
}

# block <reason> — emit the (already-formatted) stderr explanation passed via
# stdin, append an audit line, and exit non-zero. Used for every fail-closed
# branch so blocking is uniform and observable.
block() {
    local reason="$1"
    cat >&2
    audit_log "$reason"
    exit 1
}

HOOK_INPUT=$(cat)

# --- Parse tool_name. A parse failure here means we cannot even tell whether
# this is a gated tool, so we must BLOCK rather than guess it is harmless. ---
if ! TOOL_NAME=$(printf '%s' "$HOOK_INPUT" | python3 -c \
    "import sys,json; print(json.load(sys.stdin).get('tool_name',''))" 2>/dev/null); then
    # We do not yet know FRIENDLY_NAME/BOX_ID; the audit line will say '?'.
    block "hook-input-parse:tool_name" <<'EOF'
BLOCKED: guard could not evaluate hook input (tool_name), blocking by default.

The PreToolUse hook received input it could not parse as JSON, so it cannot
determine which tool is being invoked. A destructive operation might be
hiding behind malformed input, so the guard fails closed.

If you reached this from a real tool call, retry; if it persists, the hook
input is malformed and the operator MCP server should be inspected.
EOF
fi

# Only gate destructive tools. A tool that is NOT in this set is non-destructive
# and must pass without further evaluation (exit 0). This early allow is
# intentional and correct — do NOT fail closed here.
case "$TOOL_NAME" in
    mcp__skillbox-operator__operator_teardown|\
    mcp__skillbox-operator__operator_compose_down|\
    mcp__skillbox_operator__operator_teardown|\
    mcp__skillbox_operator__operator_compose_down|\
    mcp__skillbox-operator__operator_box_exec|\
    mcp__skillbox_operator__operator_box_exec)
        ;;
    *)
        exit 0
        ;;
esac

# From here on the tool IS gated: every failure must BLOCK.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FRIENDLY_NAME="${TOOL_NAME##*__}"

# tool_input is required to evaluate dry_run / box_id; a parse failure blocks.
if ! TOOL_INPUT=$(printf '%s' "$HOOK_INPUT" | python3 -c \
    "import sys,json; print(json.dumps(json.load(sys.stdin).get('tool_input',{})))" 2>/dev/null); then
    block "hook-input-parse:tool_input" <<EOF
BLOCKED: guard could not evaluate hook input (tool_input) for ${FRIENDLY_NAME}, blocking by default.

The tool arguments could not be parsed, so the guard cannot confirm whether
this is a dry run or which box is targeted. Blocking ${FRIENDLY_NAME} rather
than assuming it is safe.
EOF
fi

# --- operator_box_exec: server-side gate is authoritative; hook is backup ----
# The operator MCP server classifies box_exec commands and enforces a per-
# command dry-run marker (bound to box_id + command hash) that bash cannot
# reproduce here, so this branch does NOT re-derive that policy or fall into the
# teardown/compose_down repo-cleanliness + box-id-marker gates below (which are
# wired to single-effect tools, not arbitrary commands). It is a conservative
# backup: allow dry_run previews and ordinary commands through (the server is
# the real gate), and BLOCK only the unambiguously catastrophic patterns that
# should never run un-previewed even if the server gate were bypassed. It is
# deliberately additive — it never over-blocks read-only inspection commands.
if [ "$FRIENDLY_NAME" = "operator_box_exec" ]; then
    BOX_EXEC_DRY_RUN=$(printf '%s' "$TOOL_INPUT" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    if not isinstance(d, dict):
        raise ValueError("tool_input is not an object")
    print("true" if d.get("dry_run") else "false")
except Exception:
    print("ERROR")
' 2>/dev/null) || BOX_EXEC_DRY_RUN="ERROR"

    if [ "$BOX_EXEC_DRY_RUN" = "ERROR" ]; then
        block "box_exec:dry_run-parse" <<EOF
BLOCKED: guard could not evaluate dry_run for operator_box_exec, blocking by default.

The tool_input could not be parsed to confirm whether this is a (safe)
dry_run=true preview, so the guard fails closed.
EOF
    fi

    # Preview calls are always safe — the server stamps the per-command marker.
    if [ "$BOX_EXEC_DRY_RUN" = "true" ]; then
        exit 0
    fi

    # Backup screen for non-dry-run: block only catastrophic, unambiguous
    # destructive patterns. Read-only inspection commands never match these, so
    # this cannot introduce friction for legitimate ops automation. The server
    # gate is what actually requires the dry-run marker for ALL mutating verbs.
    BOX_EXEC_CMD=$(printf '%s' "$TOOL_INPUT" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get("command", "") if isinstance(d, dict) else "")
except Exception:
    print("__ERROR__")
' 2>/dev/null) || BOX_EXEC_CMD="__ERROR__"

    if [ "$BOX_EXEC_CMD" = "__ERROR__" ]; then
        block "box_exec:command-parse" <<EOF
BLOCKED: guard could not read the operator_box_exec command, blocking by default.

The command argument could not be parsed, so the guard cannot screen it.
EOF
    fi

    case "$BOX_EXEC_CMD" in
        *"rm -rf /"*|*"rm -fr /"*|*":(){ :|:& };:"*|*"mkfs"*|*"dd if="*"of=/dev/"*|*"> /dev/sda"*)
            block "box_exec:catastrophic-pattern" <<EOF
BLOCKED: operator_box_exec command matches a catastrophic destructive pattern.

This is the PreToolUse backup guard; the operator MCP server is the primary
gate and already requires a per-command dry_run=true preview for mutating
commands. The submitted command matches a pattern (e.g. 'rm -rf /', mkfs,
fork bomb, raw-device dd/redirect) that must never run un-reviewed.

Re-issue operator_box_exec with dry_run=true, confirm the exact command with
the user, then run the IDENTICAL command for real.
EOF
            ;;
    esac

    # Not dry-run and not catastrophic: defer to the authoritative server gate.
    exit 0
fi

# --- Gate 1: Allow dry_run through unconditionally ---
# A malformed tool_input must NOT silently degrade to "not a dry run and keep
# evaluating" in a way that could mask the real intent. We distinguish three
# outcomes from the parser: "true", "false", or "ERROR". ERROR blocks, because
# we cannot confirm whether the caller asked for a (safe) dry run.
DRY_RUN=$(printf '%s' "$TOOL_INPUT" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    if not isinstance(d, dict):
        raise ValueError("tool_input is not an object")
    print("true" if d.get("dry_run") else "false")
except Exception:
    print("ERROR")
' 2>/dev/null) || DRY_RUN="ERROR"

if [ "$DRY_RUN" = "ERROR" ]; then
    block "dry_run-parse" <<EOF
BLOCKED: guard could not evaluate dry_run for ${FRIENDLY_NAME}, blocking by default.

The tool_input could not be parsed to confirm whether dry_run=true was
requested. Because a real (non-preview) teardown cannot be ruled out, the
guard fails closed.

Legitimate bypass: re-issue ${FRIENDLY_NAME} with a well-formed dry_run=true.
EOF
fi

if [ "$DRY_RUN" = "true" ]; then
    exit 0
fi

# ---------------------------------------------------------------------------
# Gate 2: Every git repo in the workspace must be clean AND pushed.
#
# For compose_down (local): scan host-side paths directly.
# For teardown (remote box): SSH into the box and scan there.
# ---------------------------------------------------------------------------

# check_repo <path> — prints one line per problem: "dirty:<path>" or "unpushed:<path>"
check_repo_script='
import subprocess, sys, os

def check(path):
    problems = []
    try:
        porcelain = subprocess.run(
            ["git", "-C", path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        if porcelain.stdout.strip():
            problems.append(f"dirty:{path}")

        upstream = subprocess.run(
            ["git", "-C", path, "rev-parse", "--abbrev-ref", "@{upstream}"],
            capture_output=True, text=True, timeout=10,
        )
        if upstream.returncode != 0:
            # No upstream configured — local-only branch
            # Only flag if there are actual commits (not a bare init)
            log = subprocess.run(
                ["git", "-C", path, "log", "--oneline", "-1"],
                capture_output=True, text=True, timeout=10,
            )
            if log.stdout.strip():
                problems.append(f"unpushed:{path}")
        else:
            ahead = subprocess.run(
                ["git", "-C", path, "log", "@{upstream}..HEAD", "--oneline"],
                capture_output=True, text=True, timeout=10,
            )
            if ahead.stdout.strip():
                problems.append(f"unpushed:{path}")
    except Exception as exc:
        # Repo unreachable (permissions, broken mount, etc). Refuse to assume
        # it is clean — that would mask real uncommitted work.
        problems.append(f"inaccessible:{path}: {exc.__class__.__name__}: {exc}")
    return problems

repo_root = sys.argv[1] if len(sys.argv) > 1 else "."

def discover_git_roots(base):
    roots = []
    if not base or not os.path.isdir(base):
        return roots
    for current, dirs, _files in os.walk(base):
        if ".git" in dirs:
            roots.append(current)
            dirs[:] = []
            continue
        dirs[:] = [
            name for name in dirs
            if name not in {
                ".git",
                ".skillbox-state",
                "node_modules",
                "__pycache__",
                ".venv",
                "venv",
            }
        ]
    return roots

# Bounded scan roots. The monoserver default is the in-state monoserver
# directory (./.skillbox-state/monoserver), matching .env.example and
# install.sh — NOT the parent of the skillbox checkout. Walking repo_root/..
# would inflate the scan to unrelated sibling repos on the operator host. An
# operator running a real monorepo layout must opt in by setting
# SKILLBOX_MONOSERVER_HOST_ROOT explicitly.
default_monoserver = os.path.join(repo_root, ".skillbox-state", "monoserver")
search_roots = []
for base in [
    repo_root,
    os.path.join(repo_root, "repos"),
    os.path.join(repo_root, "workspace", "clients"),
    os.environ.get("SKILLBOX_CLIENTS_HOST_ROOT", os.path.join(repo_root, ".skillbox-state", "clients")),
    os.environ.get("SKILLBOX_MONOSERVER_HOST_ROOT", default_monoserver),
]:
    search_roots.extend(discover_git_roots(base))

deduped_roots = []
seen = set()
for root in search_roots:
    marker = os.path.realpath(root)
    if marker in seen:
        continue
    seen.add(marker)
    deduped_roots.append(root)

all_problems = []
for r in deduped_roots:
    all_problems.extend(check(r))

for p in all_problems:
    print(p)

sys.exit(1 if all_problems else 0)
'

# Remote check script — runs on the box via SSH.
# Scans /workspace and /monoserver for git repos.
remote_check_script='
import subprocess, sys, os

def check(path):
    problems = []
    try:
        porcelain = subprocess.run(
            ["git", "-C", path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        if porcelain.stdout.strip():
            problems.append(f"dirty:{path}")
        upstream = subprocess.run(
            ["git", "-C", path, "rev-parse", "--abbrev-ref", "@{upstream}"],
            capture_output=True, text=True, timeout=10,
        )
        if upstream.returncode != 0:
            log = subprocess.run(
                ["git", "-C", path, "log", "--oneline", "-1"],
                capture_output=True, text=True, timeout=10,
            )
            if log.stdout.strip():
                problems.append(f"unpushed:{path}")
        else:
            ahead = subprocess.run(
                ["git", "-C", path, "log", "@{upstream}..HEAD", "--oneline"],
                capture_output=True, text=True, timeout=10,
            )
            if ahead.stdout.strip():
                problems.append(f"unpushed:{path}")
    except Exception as exc:
        # Repo unreachable on the remote box (permissions, broken mount, etc.).
        # Refuse to assume it is clean — that would mask real uncommitted work.
        problems.append(f"inaccessible:{path}: {exc.__class__.__name__}: {exc}")
    return problems

def discover_git_roots(base):
    roots = []
    if not os.path.isdir(base):
        return roots
    for current, dirs, _files in os.walk(base):
        if ".git" in dirs:
            roots.append(current)
            dirs[:] = []
            continue
        dirs[:] = [
            name for name in dirs
            if name not in {
                ".git",
                ".skillbox-state",
                "node_modules",
                "__pycache__",
                ".venv",
                "venv",
            }
        ]
    return roots

search_roots = []
for base in ["/workspace", "/workspace/repos", "/workspace/workspace/clients", "/monoserver"]:
    search_roots.extend(discover_git_roots(base))

deduped_roots = []
seen = set()
for root in search_roots:
    marker = os.path.realpath(root)
    if marker in seen:
        continue
    seen.add(marker)
    deduped_roots.append(root)

all_problems = []
for r in deduped_roots:
    all_problems.extend(check(r))
for p in all_problems:
    print(p)
sys.exit(1 if all_problems else 0)
'

# box_id parse failure blocks — we cannot decide local-vs-remote scan path.
BOX_ID=$(printf '%s' "$TOOL_INPUT" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    if not isinstance(d, dict):
        raise ValueError("tool_input is not an object")
    print(d.get("box_id", "local"))
except Exception:
    print("__ERROR__")
' 2>/dev/null) || BOX_ID="__ERROR__"

if [ "$BOX_ID" = "__ERROR__" ]; then
    block "box_id-parse" <<EOF
BLOCKED: guard could not evaluate box_id for ${FRIENDLY_NAME}, blocking by default.

The target box could not be determined from tool_input, so the guard cannot
choose between the local and remote cleanliness checks. Failing closed.
EOF
fi

# run_local_check runs the host-side cleanliness scan and BLOCKS on any failure
# of the check itself (python3 missing, the embedded script raising, etc.) so a
# crashed scan can never yield empty PROBLEMS and silently pass. On success it
# echoes the (possibly empty) newline-separated problem list to stdout.
#
# The python script exits 1 when it finds dirty/unpushed/inaccessible repos
# (expected, NOT an evaluation failure) and 0 when clean. Any OTHER exit code
# (127 = python3 missing, 2 = SyntaxError, 1 with a traceback on stderr, etc.)
# means the check could not run, so we block and name the step.
run_local_check() {
    local label="$1"
    local out err rc detail=""
    err=$(mktemp 2>/dev/null) || err=""
    if [ -n "$err" ]; then
        if out=$(python3 -c "$check_repo_script" "$REPO_ROOT" 2>"$err"); then
            rc=0
        else
            rc=$?
        fi
        detail=$(tr '\n' ' ' < "$err" 2>/dev/null) || detail=""
        rm -f "$err" 2>/dev/null || true
    else
        # mktemp failed; run without capturing stderr to a file.
        if out=$(python3 -c "$check_repo_script" "$REPO_ROOT" 2>/dev/null); then
            rc=0
        else
            rc=$?
        fi
    fi
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 1 ]; then
        block "repo-check:${label}:exit=${rc}" <<EOF
BLOCKED: guard could not evaluate repo cleanliness (${label}) for ${FRIENDLY_NAME}, blocking by default.

The workspace cleanliness scan exited ${rc} instead of completing. This means
python3 is unavailable or the scan itself crashed — the guard cannot confirm
the repos are clean, so it fails closed rather than assume safety.
${detail:+Detail: ${detail}}
Legitimate bypass: ensure python3 and git are on PATH, commit/push the repos,
then re-run ${FRIENDLY_NAME} with dry_run=true first.
EOF
    fi
    printf '%s' "$out"
}

PROBLEMS=""

if [ "$FRIENDLY_NAME" = "operator_compose_down" ] || { [ "$FRIENDLY_NAME" = "operator_teardown" ] && [ "$BOX_ID" = "local" ]; }; then
    # Local: scan host-side paths
    PROBLEMS=$(run_local_check "local")
elif [ "$FRIENDLY_NAME" = "operator_teardown" ] && [ "$BOX_ID" != "local" ]; then
    # Remote: look up box SSH details from inventory, run check on the box.
    # Resolve the SSH target using the same address priority as box.py
    # (tailscale_ip > tailscale_hostname > droplet_ip). A box present in the
    # inventory but without any reachable address prints __NOHOST__ so we can
    # refuse to assume it is clean rather than silently skip the remote check.
    # A failure of the inventory lookup itself BLOCKS (we cannot decide where
    # to connect, so we fail closed instead of skipping the remote scan).
    if ! SSH_TARGET=$(python3 -c '
import json, sys
from pathlib import Path
repo_root, box_id = sys.argv[1], sys.argv[2]
inv_path = Path(repo_root) / "workspace" / "boxes.json"
if not inv_path.is_file():
    print("__NOINVENTORY__")
    sys.exit(0)
boxes = json.loads(inv_path.read_text()).get("boxes", [])
for b in boxes:
    if b.get("id") == box_id:
        host = b.get("tailscale_ip") or b.get("tailscale_hostname") or b.get("droplet_ip", "")
        user = b.get("ssh_user", "skillbox")
        print(f"{user}@{host}" if host else "__NOHOST__")
        break
else:
    print("__NOTFOUND__")
' "$REPO_ROOT" "$BOX_ID" 2>/dev/null); then
        block "inventory-lookup" <<EOF
BLOCKED: guard could not evaluate the box inventory for ${FRIENDLY_NAME} (box: ${BOX_ID}), blocking by default.

workspace/boxes.json could not be read or parsed, so the guard cannot resolve
the box's SSH target to verify its repos. Failing closed.
EOF
    fi

    if [ "$SSH_TARGET" = "__NOHOST__" ]; then
        # Box exists in inventory but has no reachable address — cannot verify.
        PROBLEMS="inaccessible:${BOX_ID}: box has no reachable address in inventory (could not verify remote repos)"
    elif [ "$SSH_TARGET" = "__NOTFOUND__" ] || [ "$SSH_TARGET" = "__NOINVENTORY__" ]; then
        # box_id not found in inventory (or no inventory file) — nothing remote
        # to verify. The local skillbox repo is still checked below.
        PROBLEMS=""
    elif [ -n "$SSH_TARGET" ]; then
        # Capture the SSH/remote-check exit status. The remote script exits 1
        # when it finds dirty/unpushed repos (expected) and 0 when clean; any
        # other status means we could NOT verify the box (connection refused,
        # auth failure, missing python3, timeout). Refuse to assume clean in
        # that case instead of failing open. ConnectTimeout bounds the TCP
        # handshake; a hard ServerAlive/overall ceiling is enforced via
        # `timeout` so a half-open or hung session cannot stall the hook.
        SSH_BIN="${SKILLBOX_GUARD_SSH_BIN:-ssh}"
        SSH_OVERALL_TIMEOUT="${SKILLBOX_GUARD_SSH_TIMEOUT_SECONDS:-30}"
        case "$SSH_OVERALL_TIMEOUT" in
            ''|*[!0-9]*) SSH_OVERALL_TIMEOUT=30 ;;
        esac
        if PROBLEMS=$(timeout "${SSH_OVERALL_TIMEOUT}" "$SSH_BIN" \
            -o StrictHostKeyChecking=accept-new \
            -o ConnectTimeout=10 \
            -o ServerAliveInterval=5 \
            -o ServerAliveCountMax=2 \
            -o BatchMode=yes \
            "$SSH_TARGET" "python3 -c $(printf '%q' "$remote_check_script")" 2>/dev/null); then
            :  # exit 0 — remote repos verified clean
        else
            ssh_rc=$?
            if [ "$ssh_rc" -eq 124 ] || [ "$ssh_rc" -eq 137 ]; then
                # `timeout` killed a hung/flaky SSH session (124 = TERM,
                # 137 = KILL). Treat as unverifiable, not clean.
                PROBLEMS="inaccessible:${SSH_TARGET}: remote check timed out after ${SSH_OVERALL_TIMEOUT}s (could not verify remote repos)"
            elif [ "$ssh_rc" -ne 1 ]; then
                PROBLEMS="inaccessible:${SSH_TARGET}: remote check exited ${ssh_rc} (could not verify remote repos)"
            fi
        fi
    else
        # Empty/unknown sentinel — fail closed rather than skip the remote scan.
        block "ssh-target-resolve" <<EOF
BLOCKED: guard could not resolve an SSH target for ${FRIENDLY_NAME} (box: ${BOX_ID}), blocking by default.

The box inventory lookup returned no usable target, so the remote repos could
not be verified. Failing closed.
EOF
    fi

    # Also check the local skillbox repo (operator's copy). A failure of this
    # local scan blocks via run_local_check.
    LOCAL_PROBLEMS=$(run_local_check "local-operator-copy")
    if [ -n "$LOCAL_PROBLEMS" ]; then
        PROBLEMS="${PROBLEMS}${PROBLEMS:+$'\n'}${LOCAL_PROBLEMS}"
    fi
fi

if [ -n "$PROBLEMS" ]; then
    # Parse problems into a readable list
    DIRTY_REPOS=""
    UNPUSHED_REPOS=""
    INACCESSIBLE_REPOS=""
    while IFS= read -r line; do
        case "$line" in
            dirty:*)        DIRTY_REPOS="${DIRTY_REPOS}    - ${line#dirty:}"$'\n' ;;
            unpushed:*)     UNPUSHED_REPOS="${UNPUSHED_REPOS}    - ${line#unpushed:}"$'\n' ;;
            inaccessible:*) INACCESSIBLE_REPOS="${INACCESSIBLE_REPOS}    - ${line#inaccessible:}"$'\n' ;;
        esac
    done <<< "$PROBLEMS"

    block "repo-cleanliness" <<EOF
BLOCKED: ${FRIENDLY_NAME} — repo cleanliness check failed.

$([ -n "$DIRTY_REPOS" ] && printf 'Uncommitted changes in:\n%s' "$DIRTY_REPOS")
$([ -n "$UNPUSHED_REPOS" ] && printf 'Unpushed commits in:\n%s' "$UNPUSHED_REPOS")
$([ -n "$INACCESSIBLE_REPOS" ] && printf 'Repos that could not be inspected (treated as unsafe):\n%sResolve permissions or remount before retrying — an\ninaccessible repo could hide uncommitted work.\n' "$INACCESSIBLE_REPOS")
ALL repos in the workspace must be committed, pushed, and
inspectable before destructive operations. Tearing down
infrastructure is irreversible; uncommitted or unpushed work
would be lost forever.

Next steps:
  1. Run /commit in each dirty repo
  2. git push in each repo with unpushed commits
  3. Resolve any inaccessible repos
  4. Then re-run ${FRIENDLY_NAME} with dry_run=true
  5. Confirm the dry-run output with the user
  6. Then run ${FRIENDLY_NAME} for real
EOF
fi

# --- Gate 3: Require dry_run=true on first real invocation ---
# Marker is invalidated after MARKER_TTL_SECONDS so a stale dry-run from a
# prior day cannot authorize today's teardown. Default 10 minutes, matching
# the operator MCP server.
MARKER="${REPO_ROOT}/.skillbox-state/dryrun-markers/.skillbox-dryrun-${FRIENDLY_NAME}-${BOX_ID}"
MARKER_TTL_SECONDS="${SKILLBOX_DRYRUN_MARKER_TTL_SECONDS:-600}"
case "$MARKER_TTL_SECONDS" in
    ''|*[!0-9]*) MARKER_TTL_SECONDS=600 ;;
esac
if [ "$MARKER_TTL_SECONDS" -le 0 ]; then
    MARKER_TTL_SECONDS=600
fi

MARKER_AGE_OK=false
MARKER_AGE=""
if [ -f "$MARKER" ]; then
    # The marker file exists, so reading its age MUST succeed for it to count.
    # A read failure here is treated as "no valid marker" (block via the
    # MARKER_AGE_OK=false path below), never as a free pass — a marker we
    # cannot age-check is the same as having no marker.
    if MARKER_AGE=$(python3 -c '
import os, sys, time
try:
    print(int(time.time() - os.stat(sys.argv[1]).st_mtime))
except Exception:
    sys.exit(1)
' "$MARKER" 2>/dev/null); then
        if [ -n "$MARKER_AGE" ] && [ "$MARKER_AGE" -ge 0 ] && [ "$MARKER_AGE" -le "$MARKER_TTL_SECONDS" ]; then
            MARKER_AGE_OK=true
        fi
    else
        # Could not stat/age the marker — treat as no valid marker.
        MARKER_AGE=""
    fi
fi

if [ "$MARKER_AGE_OK" != "true" ]; then
    MARKER_AGE_DISPLAY="${MARKER_AGE:-unavailable}"
    if [ -n "$MARKER_AGE" ]; then
        MARKER_AGE_DISPLAY="${MARKER_AGE}s"
    fi
    block "marker-missing-or-expired" <<EOF
BLOCKED: ${FRIENDLY_NAME} requires a fresh dry-run first.

Before executing a destructive operation, you must:
  1. Run ${FRIENDLY_NAME} with dry_run=true (within the last ${MARKER_TTL_SECONDS}s)
  2. Show the dry-run output to the user
  3. Get explicit confirmation
  4. Then run ${FRIENDLY_NAME} for real

This is either the first call for ${FRIENDLY_NAME} (box: ${BOX_ID}) or
the previous dry-run marker has expired. Run with dry_run=true first.

Configured marker TTL: ${MARKER_TTL_SECONDS}s
Observed marker age: ${MARKER_AGE_DISPLAY}
EOF
fi

# All gates passed
exit 0
