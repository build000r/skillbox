# Uplift Diff

## Pass 2

- `reconcile:agent-contract`: outer render/doctor entrypoint moved from argparse-only to deterministic `capabilities --json`, `robot-docs guide`, `--robot-triage`, JSON aliases, and exact typo hints.
- `box:agent-contract`: infrastructure lifecycle surface now advertises dry-run-first commands, destructive-operation metadata, MCP equivalents, JSON aliases, and exact typo hints.
- `wrapper:sbp-sbo-contract`: `sbp`/`sbo` moved from text-only wrappers with lossy flag parsing to identity-aware machine contracts with JSON/dry-run intent preserved through to `manage.py`.
- `operator-mcp:tool-contract`: MCP `tools/list` now carries machine-readable read-only/destructive hints, dry-run requirements, exact CLI equivalents, and next tools; real provision/teardown/compose-down calls are blocked until a dry-run marker exists.
- `manage:client-distribution-previews`: client and distribution lifecycle commands now have parser-valid, non-mutating preview commands in the existing capabilities contract.

No regressions observed in focused tests or the R-002 regression script.

## Pass 1

- `manage:capabilities`: new surface; self_documentation and output_parseability moved from absent/0 to high-confidence JSON contract.
- `manage:robot-docs`: new surface; external documentation lookup no longer required for agent start path.
- `manage:robot-triage`: new surface; three common inspection calls collapse into one JSON packet.
- `manage:json-aliases`: common agent typo path moved from argparse failure to inferred-and-acted.
- `manage:unknown-command`: misspelled command path now teaches the nearest valid command and points to the capabilities contract.

No regressions observed in focused tests.
