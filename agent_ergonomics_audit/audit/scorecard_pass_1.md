# Agent Ergonomics Scorecard - Pass 1

Date: 2026-06-25
Mode: full

Scores use a 1-10 scale. The pass prioritized agent-facing CLI/MCP contracts that a cold agent would run from generated context.

| Dimension | Score | Evidence | Top Fix |
|---|---:|---|---|
| First-run discoverability | 8 | `capabilities`, `robot-docs`, and README all point to the brain entrypoints. | Fixed `search` safe-first to include a query. |
| Machine-readable output | 8 | Brain happy paths and reconcile happy paths emit parseable JSON. SBP recalibrate JSON intent is still mixed output. | Defer SBP JSON envelope to `skillbox-agent-ergonomics-epic-086q.8`. |
| Structured errors | 7 | Brain engines return structured errors, but argparse still owns some surfaces. | Moved graph invalid algorithm into structured JSON; defer wider envelope to `.6`. |
| Intent recovery | 7 | `explain brain.next` works; bare command aliases were missing. | Added `explain next`/`explain snap` alias resolution; full fuzzy suggestions remain `.1`. |
| Copy-paste next actions | 8 | Registry examples are mostly runnable; some brain payloads emitted pseudo `brain.*` commands. | Centralized manage.py command hints for graph/search/snap/next payloads. |
| Safety / dry-run posture | 7 | Runtime and box capabilities advertise risk and dry-run previews. Direct box down remains too permissive. | Defer box CLI safety to `.9`. |
| Stdout/stderr separation | 8 | JSON stdout stays parseable in tested happy paths. Box read-side emits secret-location warnings on stderr. | Defer read-side warning minimization to `.9`. |
| Determinism / compactness | 8 | `--no-adapters` isolates deterministic brain output; graph/search sorting is stable. | Keep adapter-free safe-first examples. |
| Scope and idempotence | 7 | Runtime commands support client/profile/cwd scoping. Box status writes cache state despite read-side framing. | Defer status write semantics to `.9`. |
| Self-documentation | 8 | Registry drives capabilities and docs; schema drift still exists for some output declarations. | Defer registry/docs freshness to `.5` and error envelope to `.6`. |
| Regression coverage | 8 | Existing `tests/test_agent_ops_*` suites are strong; this pass added subprocess regressions for the selected fixes. | Run focused tests and required validation commands before close. |

## Ranked Findings

1. Brain payloads emitted pseudo commands (`brain.graph`, `brain.next`, `brain.search`) in next actions instead of runnable `manage.py` commands. Fixed in this pass.
2. `manage.py search` safe-first example lacked a query and failed first try. Fixed in this pass.
3. `manage.py graph --algorithm pagerank --format json` died in argparse text despite graph engine support for structured invalid-argument payloads. Fixed in this pass.
4. Bare `manage.py explain next --format json` failed with `UNKNOWN_NODE` instead of resolving to `command:brain.next`. Fixed in this pass as a conservative subset of `.1`.
5. Bare `manage.py snap --format json` died in argparse usage instead of returning agent-readable usage. Fixed in this pass as a conservative subset of `.2`.
6. `scripts/sbp recalibrate --format json` emits mixed human output. Deferred to `.8`.
7. `scripts/sbp recalibrate --auto-fix --yes` can hide heal failures with `|| true`. Deferred to `.8`.
8. Direct `scripts/box.py down` lacks a required confirmation gate. Deferred to `.9`.
9. Direct `scripts/box.py status` can mutate cached SSH target state while documented as read-side. Deferred to `.9`.
10. Brain registry output schemas drift from real payload keys and error envelopes are not unified. Deferred to `.5` and `.6`.
