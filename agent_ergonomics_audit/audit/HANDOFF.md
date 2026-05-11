# Agent Ergonomics Handoff

Pass 1 applied additive agent surfaces to `.env-manager/manage.py`.

Verification run:

- `python3 -m unittest tests.test_cli_units`
- `bash agent_ergonomics_audit/audit/regression_tests/R-001__manage_agent_surfaces.test.sh`

Important local constraints:

- The repo was dirty before this pass; preserve existing unrelated changes.
- macOS preflight lacked Linux `flock` and `timeout`, so the audit used direct focused verification.
- No branch was created. Audit workspace is in-tree.

Next pass:

1. Add equivalent `capabilities --json` / `robot-docs` contracts to `scripts/04-reconcile.py`.
2. Add equivalent contracts and JSON typo aliases to `scripts/box.py`.
3. Consider a generated `argparse` command inventory for `manage.py capabilities` so command metadata cannot drift.
