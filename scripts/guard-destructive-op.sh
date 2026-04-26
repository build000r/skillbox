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
    mcp__skillbox-operator__operator_compose_down)
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

# Find all git repos: the repo root itself, plus any .git dirs under
# workspace/repos and the monoserver mount.
search_roots = []
repo_root = sys.argv[1] if len(sys.argv) > 1 else "."

# The skillbox repo itself
if os.path.isdir(os.path.join(repo_root, ".git")):
    search_roots.append(repo_root)

# Local workspace repos dir
repos_dir = os.path.join(repo_root, "repos")
if os.path.isdir(repos_dir):
    for entry in os.listdir(repos_dir):
        candidate = os.path.join(repos_dir, entry)
        if os.path.isdir(os.path.join(candidate, ".git")):
            search_roots.append(candidate)

# Monoserver root (sibling repos mounted from host parent)
mono_root = os.environ.get("SKILLBOX_MONOSERVER_HOST_ROOT", os.path.join(repo_root, ".."))
if os.path.isdir(mono_root):
    for entry in os.listdir(mono_root):
        candidate = os.path.join(mono_root, entry)
        if os.path.isdir(os.path.join(candidate, ".git")):
            search_roots.append(candidate)

all_problems = []
for r in search_roots:
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
    except Exception:
        pass
    return problems

search_roots = []
for base in ["/workspace", "/workspace/repos", "/monoserver"]:
    if not os.path.isdir(base):
        continue
    if os.path.isdir(os.path.join(base, ".git")):
        search_roots.append(base)
    for entry in os.listdir(base):
        candidate = os.path.join(base, entry)
        if os.path.isdir(os.path.join(candidate, ".git")):
            search_roots.append(candidate)

all_problems = []
for r in search_roots:
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

if [ "$FRIENDLY_NAME" = "operator_compose_down" ]; then
    # Local: scan host-side paths
    PROBLEMS=$(python3 -c "$check_repo_script" "$REPO_ROOT" 2>/dev/null || true)
elif [ "$FRIENDLY_NAME" = "operator_teardown" ] && [ "$BOX_ID" != "local" ]; then
    # Remote: look up box SSH details from inventory, run check on the box
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
        host = b.get("tailscale_hostname") or b.get("droplet_ip", "")
        user = b.get("ssh_user", "skillbox")
        if host:
            print(f"{user}@{host}")
        break
' "$REPO_ROOT" "$BOX_ID" 2>/dev/null || true)

    if [ -n "$SSH_TARGET" ]; then
        PROBLEMS=$(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o BatchMode=yes \
            "$SSH_TARGET" "python3 -c $(printf '%q' "$remote_check_script")" 2>/dev/null || true)
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
# prior day cannot authorize today's teardown. Default 1 hour.
MARKER="${REPO_ROOT}/.skillbox-state/dryrun-markers/.skillbox-dryrun-${FRIENDLY_NAME}-${BOX_ID}"
MARKER_TTL_SECONDS="${SKILLBOX_DRYRUN_MARKER_TTL_SECONDS:-3600}"

MARKER_AGE_OK=false
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
    cat >&2 <<EOF
BLOCKED: ${FRIENDLY_NAME} requires a fresh dry-run first.

Before executing a destructive operation, you must:
  1. Run ${FRIENDLY_NAME} with dry_run=true (within the last ${MARKER_TTL_SECONDS}s)
  2. Show the dry-run output to the user
  3. Get explicit confirmation
  4. Then run ${FRIENDLY_NAME} for real

This is either the first call for ${FRIENDLY_NAME} (box: ${BOX_ID}) or
the previous dry-run marker has expired. Run with dry_run=true first.
EOF
    exit 1
fi

# All gates passed
exit 0
