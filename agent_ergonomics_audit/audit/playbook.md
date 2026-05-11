# Agent Ergonomics Playbook

## Applied

Pass 2 extended first-try agent surfaces across the surrounding CLI/MCP surface:

- `scripts/04-reconcile.py` now has `capabilities --json`, `robot-docs guide`, `--robot-triage`, JSON typo aliases, exact command hints, concise runtime errors, and skill-repo dry-run skip-code parity.
- `scripts/box.py` now has deterministic `capabilities --json`, `robot-docs guide`, `--robot-triage`, JSON typo aliases, exact command hints, dry-run-first metadata, and MCP equivalents.
- `scripts/sbp` and `scripts/sbo` now expose wrapper capabilities/robot docs/triage, preserve JSON and dry-run intent through to `manage.py`, keep typo diagnostics on stderr, and make `sbo --help` identify itself as `sbo`.
- `scripts/operator_mcp_server.py` now exposes read-only/destructive hints and `x_skillbox_contract` metadata in `tools/list`, sort-keys JSON content, gives exact next actions for unknown or missing tool inputs, and blocks real provision/teardown/compose-down calls until a dry-run marker exists.
- `.env-manager/manage.py capabilities --json` now advertises outer entrypoint contracts and parser-valid, non-mutating client/distribution preview commands.

R-001 added the missing first-try agent surfaces to the primary runtime CLI:

- `capabilities --json` exposes the machine-readable command contract.
- `robot-docs guide` gives an in-tool agent handbook with safe mutation patterns.
- `--robot-triage` returns quick recommendations, commands, and graph health in one JSON call.
- `--json`, `--jsno`, `--jason`, and `--jsson` normalize to `--format json` while keeping stdout parseable.
- Unknown top-level commands now suggest the nearest valid command and point agents at `capabilities --json`.

## Deferred

- Split exit code `2` so argparse usage errors and drift detection are distinct in every published contract.
- Replace static command metadata with generated `argparse`/schema inventory if these surfaces grow enough that drift becomes likely.
- Consider a first-class non-interactive `box.py ssh --command ... --format json` wrapper if agents need a direct CLI alternative to MCP `operator_box_exec`.
