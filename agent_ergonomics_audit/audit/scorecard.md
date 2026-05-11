# Agent Ergonomics Scorecard

Primary target: full Skillbox agent-facing CLI/MCP surface.

## Pass 2

| Surface | Pre-pass finding | Post-pass state |
| --- | --- | --- |
| `scripts/04-reconcile.py` machine contract | Only `render`/`doctor` argparse help | `capabilities --json`, `robot-docs guide`, `--robot-triage`, JSON aliases, exact typo hints, and concise runtime errors. |
| `scripts/box.py` machine contract | Lifecycle commands had JSON modes but no discovery contract | Deterministic capabilities/robot docs/triage with dry-run-first safety and MCP equivalents. |
| `scripts/sbp`/`scripts/sbo` wrappers | `sbo` identified as `sbp`; unknown commands fell through to home; JSON/dry-run flags could be lost | Wrapper identity, capabilities/robot docs/triage, exact unknown/log errors, JSON typo routing, and runtime dry-run forwarding. |
| Operator MCP `tools/list` | Prose safety warnings only | `annotations` plus `x_skillbox_contract` expose read-only/destructive hints, dry-run requirements, exact CLI, and next tools; real provision/teardown/compose-down calls require prior dry-run markers. |
| Client/distribution lifecycle discovery | Mostly hidden behind `--help` | `manage.py capabilities --json` now advertises parser-valid, non-mutating preview commands for client project/diff and distribution preview/publish/rollback. |
| stdout/stderr split | Mixed by surface | Regression-pinned JSON stdout and stderr diagnostics for reconcile, box, and wrapper aliases. |
| Destructive-operation pedagogy | Inconsistent by surface | `box.py`, wrappers, and MCP metadata all point at dry-run-first commands; MCP handlers also block real provision/teardown/compose-down calls until a dry-run marker exists. |
| Non-TTY wrapper behavior | Pipe/agent invocations could lose flags | Wrapper tests use non-interactive subprocess/fake manage.py to prove JSON and dry-run flags survive without a TTY. |

Median scored surface uplift estimate: +220 points across the Pass 2 surfaces.

## Pass 1 Baseline

| Surface | Pre-pass finding | Post-pass state |
| --- | --- | --- |
| `manage.py` machine-readable contract | Missing | `capabilities --json` returns tool, commands, entrypoints, exit codes, env vars, and next actions. |
| `manage.py` in-tool agent guide | Missing | `robot-docs guide` gives start commands, structured-output rules, and safe mutation guidance. |
| `manage.py` mega-command | Missing | `--robot-triage` returns quick ref, recommendations, commands, and graph health. |
| `manage.py` JSON typo inference | Missing | `--json`, `--jsno`, `--jason`, and `--jsson` normalize to `--format json`; typo notices go to stderr. |
| `manage.py` unknown command pedagogy | Generic argparse error | Adds capabilities hint and nearest-command suggestion. |
