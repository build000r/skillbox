#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
cd "${repo_root}"

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

assert_json() {
  python3 -m json.tool "$1" >/dev/null
}

python3 .env-manager/manage.py capabilities --json >"${tmpdir}/manage-capabilities.json"
assert_json "${tmpdir}/manage-capabilities.json"
python3 - "${tmpdir}/manage-capabilities.json" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.load(open(sys.argv[1], encoding="utf-8"))
robot_docs = next(item for item in payload["commands"] if item["name"] == "robot-docs")
assert robot_docs["json"] is True
assert "python3 scripts/04-reconcile.py capabilities --json" == payload["agent_surfaces"]["outer_reconcile"]
assert any("client-project <client> --dry-run" in item for item in payload["safe_previews"])
assert any("distribution-preview --manifest-path <manifest.json> --public-key <public-key.pem>" in item for item in payload["safe_previews"])
PY

python3 scripts/04-reconcile.py capabilities --json >"${tmpdir}/reconcile-a.json"
python3 scripts/04-reconcile.py capabilities --json >"${tmpdir}/reconcile-b.json"
cmp "${tmpdir}/reconcile-a.json" "${tmpdir}/reconcile-b.json"
assert_json "${tmpdir}/reconcile-a.json"

python3 scripts/04-reconcile.py render --jsno \
  >"${tmpdir}/reconcile-alias.json" 2>"${tmpdir}/reconcile-alias.err"
assert_json "${tmpdir}/reconcile-alias.json"
grep -q "Interpreting --jsno as --format json" "${tmpdir}/reconcile-alias.err"

if python3 scripts/04-reconcile.py doctro --json >"${tmpdir}/bad.out" 2>"${tmpdir}/bad.err"; then
  echo "expected 04-reconcile typo to fail" >&2
  exit 1
fi
test ! -s "${tmpdir}/bad.out"
grep -q "Did you mean: \`04-reconcile.py doctor\`" "${tmpdir}/bad.err"

python3 scripts/box.py capabilities --json >"${tmpdir}/box-a.json"
python3 scripts/box.py capabilities --json >"${tmpdir}/box-b.json"
cmp "${tmpdir}/box-a.json" "${tmpdir}/box-b.json"
assert_json "${tmpdir}/box-a.json"
python3 - "${tmpdir}/box-a.json" <<'PY'
from __future__ import annotations

import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
down = next(item for item in payload["commands"] if item["name"] == "down")
assert down["destructive"] is True
assert down["dry_run"] is True
assert "python3 scripts/box.py down <box-id> --dry-run --format json" in payload["safety"]["dry_run_first"]
PY

python3 scripts/box.py profiles --jsno >"${tmpdir}/box-alias.json" 2>"${tmpdir}/box-alias.err"
assert_json "${tmpdir}/box-alias.json"
grep -q "Interpreting --jsno as --format json" "${tmpdir}/box-alias.err"

if python3 scripts/box.py statuz --json >"${tmpdir}/box-bad.out" 2>"${tmpdir}/box-bad.err"; then
  echo "expected box typo to fail" >&2
  exit 1
fi
test ! -s "${tmpdir}/box-bad.out"
grep -q "Did you mean: \`box.py status\`" "${tmpdir}/box-bad.err"

fake_root="${tmpdir}/fake-skillbox"
mkdir -p "${fake_root}/.env-manager"
cat >"${fake_root}/.env-manager/manage.py" <<'PY'
from __future__ import annotations

import json
import os
import sys

payload = {"argv": sys.argv[1:], "cwd": os.getcwd()}
with open(os.environ["SKILLBOX_RECORD"], "w", encoding="utf-8") as handle:
    json.dump(payload, handle)
print(json.dumps(payload, sort_keys=True))
PY

SKILLBOX_ROOT="${fake_root}" bash scripts/sbp capabilities --json >"${tmpdir}/sbp-capabilities.json"
assert_json "${tmpdir}/sbp-capabilities.json"
SKILLBOX_ROOT="${fake_root}" bash scripts/sbo --help >"${tmpdir}/sbo-help.txt"
grep -q "sbo capabilities --json" "${tmpdir}/sbo-help.txt"

SKILLBOX_ROOT="${fake_root}" SKILLBOX_RECORD="${tmpdir}/record.json" \
  bash scripts/sbp status core --jsno >"${tmpdir}/sbp-status.json" 2>"${tmpdir}/sbp-status.err"
assert_json "${tmpdir}/sbp-status.json"
grep -q "Interpreting --jsno as --format json" "${tmpdir}/sbp-status.err"
python3 - "${tmpdir}/record.json" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload["argv"] == [
    "status",
    "--cwd",
    str(Path.cwd()),
    "--profile",
    "local-core",
    "--format",
    "json",
]
PY

SKILLBOX_ROOT="${fake_root}" SKILLBOX_RECORD="${tmpdir}/record.json" \
  bash scripts/sbp up backend spaps --dry-run --json >/dev/null
python3 - "${tmpdir}/record.json" <<'PY'
from __future__ import annotations

import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert "--dry-run" in payload["argv"]
assert "--service" in payload["argv"]
assert "spaps" in payload["argv"]
PY

if SKILLBOX_ROOT="${fake_root}" bash scripts/sbp statu >"${tmpdir}/sbp-bad.out" 2>"${tmpdir}/sbp-bad.err"; then
  echo "expected sbp typo to fail" >&2
  exit 1
fi
test ! -s "${tmpdir}/sbp-bad.out"
grep -q "Did you mean: sbp status --json" "${tmpdir}/sbp-bad.err"

python3 - <<'PY'
from __future__ import annotations

import json
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

root = Path.cwd()
module = SourceFileLoader("skillbox_operator_mcp_regression", str(root / "scripts" / "operator_mcp_server.py")).load_module()
teardown = next(item for item in module.TOOLS if item["name"] == "operator_teardown")
assert teardown["annotations"]["destructiveHint"] is True
assert teardown["x_skillbox_contract"]["dry_run_required"] is True
assert teardown["x_skillbox_contract"]["safe_first_call"] == "operator_teardown(box_id='<id>', dry_run=true)"
missing = json.loads(module.dispatch_tool("missing", {})["content"][0]["text"])
assert "operator_boxes" in missing["error"]["next_actions"]
with tempfile.TemporaryDirectory() as tmp:
    module.REPO_ROOT = Path(tmp)
    blocked = json.loads(module.handle_operator_compose_down({})["content"][0]["text"])
    assert blocked["error"]["type"] == "dry_run_required"
PY

echo "R-002 full-surface agent contracts regression passed"
