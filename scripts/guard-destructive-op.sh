#!/usr/bin/env bash
# guard-destructive-op.sh — PreToolUse hook that gates destructive MCP tools.
#
# Blocks operator_teardown and operator_compose_down unless:
#   1. dry_run=true (preview mode always passes)
#   2. ALL git repos in the workspace are clean and pushed
#   3. A dry-run was already executed this session
#
# Checks the skillbox repo itself AND every git repo under the client
# workspace roots (/workspace/repos, /monoserver/*, cloned client repos).
#
# Input: JSON on stdin (Claude Code hook protocol)

set -euo pipefail

HOOK_INPUT=$(cat)

TOOL_NAME=$(echo "$HOOK_INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_name',''))" 2>/dev/null || echo "")
TOOL_INPUT=$(echo "$HOOK_INPUT" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get('tool_input',{})))" 2>/dev/null || echo "{}")

# Only gate destructive tools
case "$TOOL_NAME" in
    mcp__skillbox-operator__operator_teardown|\
    mcp__skillbox-operator__operator_compose_down|\
    mcp__skillbox_operator__operator_teardown|\
    mcp__skillbox_operator__operator_compose_down)
        ;;
    *)
        exit 0
        ;;
esac

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FRIENDLY_NAME="${TOOL_NAME##*__}"

# --- Gate 1: Allow dry_run through unconditionally ---
DRY_RUN=$(echo "$TOOL_INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print('true' if d.get('dry_run') else 'false')
except:
    print('false')
" 2>/dev/null || echo "false")

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

search_roots = []
for base in [
    repo_root,
    os.path.join(repo_root, "repos"),
    os.path.join(repo_root, "workspace", "clients"),
    os.environ.get("SKILLBOX_CLIENTS_HOST_ROOT", os.path.join(repo_root, ".skillbox-state", "clients")),
    os.environ.get("SKILLBOX_MONOSERVER_HOST_ROOT", os.path.join(repo_root, "..")),
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

BOX_ID=$(echo "$TOOL_INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('box_id', 'local'))
except:
    print('local')
" 2>/dev/null || echo "local")

PROBLEMS=""

if [ "$FRIENDLY_NAME" = "operator_compose_down" ] || { [ "$FRIENDLY_NAME" = "operator_teardown" ] && [ "$BOX_ID" = "local" ]; }; then
    # Local: scan host-side paths
    PROBLEMS=$(python3 -c "$check_repo_script" "$REPO_ROOT" 2>/dev/null || true)
elif [ "$FRIENDLY_NAME" = "operator_teardown" ] && [ "$BOX_ID" != "local" ]; then
    # Remote: look up box SSH details from inventory, run check on the box
    # Resolve the SSH target using the same address priority as box.py
    # (tailscale_ip > tailscale_hostname > droplet_ip). A box present in the
    # inventory but without any reachable address prints __NOHOST__ so we can
    # refuse to assume it is clean rather than silently skip the remote check.
    SSH_TARGET=$(python3 -c '
import json, sys
from pathlib import Path
repo_root, box_id = sys.argv[1], sys.argv[2]
inv_path = Path(repo_root) / "workspace" / "boxes.json"
if not inv_path.is_file():
    sys.exit(0)
boxes = json.loads(inv_path.read_text()).get("boxes", [])
for b in boxes:
    if b.get("id") == box_id:
        host = b.get("tailscale_ip") or b.get("tailscale_hostname") or b.get("droplet_ip", "")
        user = b.get("ssh_user", "skillbox")
        print(f"{user}@{host}" if host else "__NOHOST__")
        break
' "$REPO_ROOT" "$BOX_ID" 2>/dev/null || true)

    if [ "$SSH_TARGET" = "__NOHOST__" ]; then
        # Box exists in inventory but has no reachable address — cannot verify.
        PROBLEMS="inaccessible:${BOX_ID}: box has no reachable address in inventory (could not verify remote repos)"
    elif [ -n "$SSH_TARGET" ]; then
        # Capture the SSH/remote-check exit status. The remote script exits 1
        # when it finds dirty/unpushed repos (expected) and 0 when clean; any
        # other status means we could NOT verify the box (connection refused,
        # auth failure, missing python3, timeout). Refuse to assume clean in
        # that case instead of failing open.
        if PROBLEMS=$(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o BatchMode=yes \
            "$SSH_TARGET" "python3 -c $(printf '%q' "$remote_check_script")" 2>/dev/null); then
            :  # exit 0 — remote repos verified clean
        else
            ssh_rc=$?
            if [ "$ssh_rc" -ne 1 ]; then
                PROBLEMS="inaccessible:${SSH_TARGET}: remote check exited ${ssh_rc} (could not verify remote repos)"
            fi
        fi
    else
        # box_id not found in inventory — nothing remote to verify.
        PROBLEMS=""
    fi

    # Also check the local skillbox repo (operator's copy)
    LOCAL_PROBLEMS=$(python3 -c "$check_repo_script" "$REPO_ROOT" 2>/dev/null || true)
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

    {
        echo "BLOCKED: ${FRIENDLY_NAME} — repo cleanliness check failed."
        echo ""
        if [ -n "$DIRTY_REPOS" ]; then
            echo "Uncommitted changes in:"
            echo "$DIRTY_REPOS"
        fi
        if [ -n "$UNPUSHED_REPOS" ]; then
            echo "Unpushed commits in:"
            echo "$UNPUSHED_REPOS"
        fi
        if [ -n "$INACCESSIBLE_REPOS" ]; then
            echo "Repos that could not be inspected (treated as unsafe):"
            echo "$INACCESSIBLE_REPOS"
            echo "Resolve permissions or remount before retrying — an"
            echo "inaccessible repo could hide uncommitted work."
            echo ""
        fi
        echo "ALL repos in the workspace must be committed, pushed, and"
        echo "inspectable before destructive operations. Tearing down"
        echo "infrastructure is irreversible; uncommitted or unpushed work"
        echo "would be lost forever."
        echo ""
        echo "Next steps:"
        echo "  1. Run /commit in each dirty repo"
        echo "  2. git push in each repo with unpushed commits"
        echo "  3. Resolve any inaccessible repos"
        echo "  4. Then re-run ${FRIENDLY_NAME} with dry_run=true"
        echo "  5. Confirm the dry-run output with the user"
        echo "  6. Then run ${FRIENDLY_NAME} for real"
    } >&2
    exit 1
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
    MARKER_AGE=$(python3 -c '
import os, sys, time
try:
    print(int(time.time() - os.stat(sys.argv[1]).st_mtime))
except OSError:
    sys.exit(1)
' "$MARKER" 2>/dev/null || echo "")
    if [ -n "$MARKER_AGE" ] && [ "$MARKER_AGE" -ge 0 ] && [ "$MARKER_AGE" -le "$MARKER_TTL_SECONDS" ]; then
        MARKER_AGE_OK=true
    fi
fi

if [ "$MARKER_AGE_OK" != "true" ]; then
    MARKER_AGE_DISPLAY="${MARKER_AGE:-unavailable}"
    if [ -n "$MARKER_AGE" ]; then
        MARKER_AGE_DISPLAY="${MARKER_AGE}s"
    fi
    cat >&2 <<EOF
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
    exit 1
fi

# All gates passed
exit 0
