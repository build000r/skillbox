# Agent Ergonomics Playbook

## Applied

R-001 added the missing first-try agent surfaces to the primary runtime CLI:

- `capabilities --json` exposes the machine-readable command contract.
- `robot-docs guide` gives an in-tool agent handbook with safe mutation patterns.
- `--robot-triage` returns quick recommendations, commands, and graph health in one JSON call.
- `--json`, `--jsno`, `--jason`, and `--jsson` normalize to `--format json` while keeping stdout parseable.
- Unknown top-level commands now suggest the nearest valid command and point agents at `capabilities --json`.

## Deferred

- Extend the same `capabilities`/`robot-docs` contract to `scripts/04-reconcile.py` and `scripts/box.py`.
- Split exit code `2` so argparse usage errors and drift detection are distinct in the published contract.
- Add generated command metadata from `argparse` instead of the current static command inventory.
