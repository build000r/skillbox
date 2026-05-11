# Agent Ergonomics Handoff

Pass 2 extended the Pass 1 `manage.py` agent surfaces across the full operator/runtime loop:

- `scripts/04-reconcile.py`: `capabilities --json`, `robot-docs guide`, `--robot-triage`, JSON typo aliases, exact suggestions, concise runtime errors, and `skill-repo-sync-dry-run` skip-code parity.
- `scripts/box.py`: deterministic capabilities/robot docs/triage, JSON typo aliases, exact suggestions, dry-run-first safety metadata, and MCP equivalents.
- `scripts/sbp` / `scripts/sbo`: wrapper capabilities/robot docs/triage, identity-aware `sbo --help`, JSON alias forwarding, runtime dry-run flag routing, and exact unknown/logs errors.
- `scripts/operator_mcp_server.py`: `annotations` and `x_skillbox_contract` metadata, deterministic JSON content, exact next actions for unknown/missing inputs, provision dry-run marker stamping, and dry-run-required guards for real mutation calls.
- `.env-manager/manage.py capabilities --json`: robot-docs JSON correction plus parser-valid, non-mutating client/distribution lifecycle preview commands.

Focused verification run:

- `python3 -m unittest tests.test_reconcile`
- `python3 -m unittest tests.test_box`
- `python3 -m unittest tests.test_cli_wrappers`
- `python3 -m unittest tests.test_operator_mcp_server tests.test_cli_units`
- `bash agent_ergonomics_audit/audit/regression_tests/R-002__full_surface_agent_contracts.test.sh`

Important local constraints:

- The repo was dirty before this pass with `.beads/issues.jsonl` and untracked `.buildooor/`; preserve that unrelated local state unless explicitly scoped.
- macOS preflight still lacked Linux `flock` and `timeout`, so direct focused verification remains the reliable local proof path.
- No branch or sibling audit workspace was created. Audit workspace remains in-tree.

Next pass:

1. Generate command metadata from argparse/schema definitions if static capabilities inventory starts to drift.
2. Consider `box.py ssh --command ... --format json` only if agents need a direct CLI alternative to MCP `operator_box_exec`.
3. Split exit code `2` semantics across the published contracts if callers need programmatic usage-error versus drift separation.
