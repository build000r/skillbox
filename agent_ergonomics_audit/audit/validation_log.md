# Validation Log

Date: 2026-06-25

## Required CLI Commands

- `python3 .env-manager/manage.py capabilities --format json`: passed, parseable JSON.
- `python3 .env-manager/manage.py next --format json`: passed, parseable JSON.
- `python3 .env-manager/manage.py graph --format json`: passed, parseable JSON.
- `python3 .env-manager/manage.py explain brain.next --format json`: passed, parseable JSON.
- `python3 .env-manager/manage.py search "doctor" --format json`: passed, parseable JSON.

## Focused Tests

- `python3 -m unittest tests.test_agent_ops_decisions tests.test_cli_units`: passed, 46 tests.
- `python3 -m unittest tests.test_agent_ops_decisions tests.test_agent_ops_graph_engine tests.test_agent_ops_search tests.test_agent_ops_snapshots tests.test_cli_units`: passed, 62 tests.
- `python3 -m unittest tests.test_agent_ops_adapters tests.test_agent_ops_command_registry tests.test_agent_ops_graph tests.test_agent_ops_graph_algorithms tests.test_agent_ops_graph_engine tests.test_agent_ops_decisions tests.test_agent_ops_search tests.test_agent_ops_snapshots tests.test_agent_ops_golden_outputs tests.test_cli_units`: passed, 115 tests in 43.301s.

## Broad Tests

- `python3 -m unittest discover -s tests`: passed, 1,780 tests in 732.541s, 4 skipped.

Notes: broad run emitted expected warning/error-path fixture output, including deprecated operator secret location warnings captured as a deferred `box.py` ergonomics finding in `skillbox-agent-ergonomics-epic-086q.9`.
