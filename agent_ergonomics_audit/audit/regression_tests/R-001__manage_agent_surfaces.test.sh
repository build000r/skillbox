#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$repo_root"

python3 .env-manager/manage.py capabilities --json | jq -e '
  .tool == "skillbox-manage"
  and (.agent_surfaces.capabilities | contains("capabilities --json"))
  and (.agent_surfaces.robot_triage | contains("--robot-triage"))
  and (.commands[] | select(.name == "status"))
' >/dev/null

python3 .env-manager/manage.py --robot-triage | jq -e '
  .tool == "skillbox-manage"
  and .health.model_loaded == true
  and (.commands.status | contains("status --format json"))
' >/dev/null

python3 .env-manager/manage.py status --jsno > /tmp/skillbox-agent-json.out 2> /tmp/skillbox-agent-json.err
jq -e '.active_profiles' /tmp/skillbox-agent-json.out >/dev/null
grep -q 'Interpreting --jsno as --format json' /tmp/skillbox-agent-json.err

if python3 .env-manager/manage.py statu --json > /tmp/skillbox-agent-bad.out 2> /tmp/skillbox-agent-bad.err; then
  echo "expected misspelled command to fail" >&2
  exit 1
fi
test ! -s /tmp/skillbox-agent-bad.out
grep -q 'Did you mean: `manage.py status`?' /tmp/skillbox-agent-bad.err
grep -q 'manage.py capabilities --json' /tmp/skillbox-agent-bad.err
