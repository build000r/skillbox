# Agent Ergonomics Handoff

Date: 2026-06-25
Active bead: `skillbox-agent-ergonomics-epic-086q.7`

## Applied In This Pass

- Added copy-pasteable brain command hints via `agent_cli_hints.py`.
- Fixed `capabilities` search safe-first command.
- Added exact bare command aliases for `explain next` and related brain commands.
- Moved invalid graph algorithm JSON calls into the graph engine's structured `INVALID_ARGUMENT` payload.
- Added structured `snap --format json` usage when no action is supplied.
- Added focused regression coverage in `tests/test_agent_ops_*` and `tests/test_cli_units.py`.

## Deferred Beads

- `skillbox-agent-ergonomics-epic-086q.1`: full fuzzy suggestions, ambiguity handling, and broader explain/graph/search recovery.
- `skillbox-agent-ergonomics-epic-086q.2`: full snap flag-position tolerance, text-mode usage policy, registry example refresh, and MCP parity.
- `skillbox-agent-ergonomics-epic-086q.5`: generated API reference and registry/docs freshness.
- `skillbox-agent-ergonomics-epic-086q.6`: unified error envelope across brain surfaces and MCP mirrors; also covers output schema drift.
- `skillbox-agent-ergonomics-epic-086q.8`: SBP wrapper JSON/mutation contract hardening.
- `skillbox-agent-ergonomics-epic-086q.9`: direct `box.py` safety, status write semantics, and JSON argparse errors.

## Verification To Run Before Closing

- `python3 .env-manager/manage.py capabilities --format json`
- `python3 .env-manager/manage.py next --format json`
- `python3 .env-manager/manage.py graph --format json`
- `python3 .env-manager/manage.py explain brain.next --format json`
- `python3 .env-manager/manage.py search "doctor" --format json`
- `python3 -m unittest tests.test_agent_ops_adapters tests.test_agent_ops_command_registry tests.test_agent_ops_graph tests.test_agent_ops_graph_algorithms tests.test_agent_ops_graph_engine tests.test_agent_ops_decisions tests.test_agent_ops_search tests.test_agent_ops_snapshots tests.test_agent_ops_golden_outputs tests.test_cli_units`

Run full `python3 -m unittest discover -s tests` only if time permits. The repo has unrelated dirty local/generated state; commit with a temporary index and include only intended files.
