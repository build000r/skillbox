# Skillbox Profiling And Refactor Candidate Plan - 2026-05-28

Scope: planning-only output for `skillbox-portfolio-reality-idea-plan-h1k.4`.
No profiling instrumentation, optimization, or refactor code changes were made.
This is dated evidence from 2026-05-28; later push-readiness checks should be
treated as the current source of truth.

## Ground Rules

- No optimization without a scenario, baseline fingerprint, hotspot table, and hypothesis ledger.
- No simplification without a green baseline, golden output comparison, and isomorphism proof card.
- At the time of this pass, disk pressure was critical, so expensive broad profiling and broad test loops should wait for `skillbox-smart-20260518-validation-trust-gkn` or an approved offload lane.
- Future artifacts should live under `tests/artifacts/perf/<run-id>/` for profiling and `refactor/artifacts/<run-id>/` for isomorphic refactors.

## Cheap Signals Read

- `agent_ergonomics_audit/AUDIT_2026-05-17.md`
- `docs/status/skillbox-reality-check-2026-05-28.md`
- `docs/status/skillbox-idea-slate-2026-05-28.md`
- `docs/status/skillbox-power-map-2026-05-28.md`
- Source shape scan with `wc -l` and targeted reads of `scripts/box.py`, `.env-manager/pulse.py`, `runtime_ops.py`, `workflows.py`, `skill_visibility.py`, and `context_rendering.py`

Large current modules:

| Path | Lines | Read |
|---|---:|---|
| `.env-manager/runtime_manager/runtime_ops.py` | 4740 | Runtime status, pressure, lifecycle, and rendering support live together. |
| `.env-manager/runtime_manager/workflows.py` | 4034 | Focus, stewardship, acceptance, and report workflows. |
| `.env-manager/runtime_manager/cli.py` | 3643 | Parser and command handlers. |
| `.env-manager/runtime_manager/skill_visibility.py` | large | Skill visibility, lifecycle planning, audit, and printing. |
| `scripts/box.py` | 3775 | Box lifecycle, status, SSH, storage, registration. |
| `.env-manager/pulse.py` | 1115 | Reconciliation loop and pressure/status observation. |

Stale audit note: the older `box status` serial health and SSH candidate serial-probe findings appear already implemented now. `cmd_status` uses `ThreadPoolExecutor(max_workers=min(5, len(active_boxes)))`, `resolve_box_ssh_target` probes remaining candidates in parallel after cached-target preference, and `tests/test_box_refactor.py` covers both overlap behaviors. Do not mint those as new performance Beads unless fresh measurement finds a remaining issue.

## Profiling Candidates

| Rank | Candidate | Scenario | Required artifacts | Future validation |
|---:|---|---|---|---|
| 1 | Runtime proof/status packet latency | Run `manage.py status --profile pressure-tools --format json --compact`, `manage.py doctor --format json`, and the proposed evidence-packet command once it exists. Measure wall time, p95 over 20 runs, and cProfile cumulative time. | `fingerprint.json`, `baseline-status.json`, `cprofile-status.pstats`, `hotspot-table.md`, `hypothesis-ledger.md` | `python3 .env-manager/manage.py status --profile pressure-tools --format json --compact`; `make doctor`; focused runtime status tests. |
| 2 | Skill visibility and audit scan cost | Run `manage.py skills --issues-only --cwd <repo> --format json` and `manage.py skill-audit --scan-root ~/repos --limit 20 --format json`. Measure filesystem walk, global/project skill layer collection, and candidate repo scan time. | `fingerprint.json`, `baseline-skills.json`, `cprofile-skills.pstats`, `scan-counts.json`, `hotspot-table.md` | `python3 .env-manager/manage.py skills --issues-only --cwd <repo> --format json`; `python3 .env-manager/manage.py skill-audit --cwd <repo> --format json --limit 20`; `python3 -m unittest tests.test_cli_units tests.test_mcp_server`. |
| 3 | Pulse reconciliation cycle cost | Measure one no-infra pulse reconciliation cycle against a fixture model: service snapshot, check snapshot, pressure advisory collection, and state write. Track cycle duration and pressure-advisory cost separately. | `fingerprint.json`, `pulse-cycle-baseline.json`, `span-summary.json`, `hotspot-table.md`, `hypothesis-ledger.md` | `python3 -m unittest tests.test_pulse tests.test_pressure_visibility`; `python3 .env-manager/pulse.py status`. |
| 4 | Context generation and focus context writes | Measure `generate_context_markdown`, `generate_skill_context`, and dry-run `manage.py context --dry-run --format json` on core plus a client fixture. Include output byte size and golden hashes. | `fingerprint.json`, `baseline-context.json`, `golden-context.sha256`, `cprofile-context.pstats`, `hotspot-table.md` | `python3 .env-manager/manage.py context --dry-run --format json`; `python3 -m unittest tests.test_runtime_hotspot_units tests.test_ios_project_contract`. |
| 5 | Stewardship/report workflow cost | Measure `stewardship-report` once client activation is available or against fixture data. The target is future evidence-packet reuse, not optimization first. | `fingerprint.json`, `baseline-stewardship.json`, `report-golden.sha256`, `hotspot-table.md` | Focused workflow tests plus the future evidence packet command. |
| 6 | Worker broker context resolution smoke | Measure `worker-submit` with a fake/no-op Hermes runtime in a temp state root, not a real Codex worker launch. Attribute context resolution, state write, and result read costs. | `fingerprint.json`, `baseline-worker-fake.json`, `state-tree.txt`, `hotspot-table.md` | `python3 -m unittest tests.test_worker_runtime`; fake-runtime smoke only. |
| 7 | RCH/SBH report timeout posture | Measure `rch-report` and `sbh-report` with absent and configured binaries. This is mostly timeout discipline, not CPU optimization. | `fingerprint.json`, `baseline-pressure-tools.json`, `timeout-matrix.md` | `python3 .env-manager/manage.py rch-report --format json`; `python3 .env-manager/manage.py sbh-report --format json`; focused pressure tests. |

Rejected for this candidate plan:

- `box status` parallelization and SSH candidate parallel probing: already implemented and tested; keep as regression surfaces only.
- Distributor signed-bundle sync performance: separate distribution epic, not the current reliability wave.
- Full broad unittest runtime optimization: first restore validation trust and disk-safe execution; then measure.

## Isomorphic Refactor Candidates

| Rank | Candidate | Why consider it | Isomorphism proof required | Future validation |
|---:|---|---|---|---|
| 1 | Extract a reusable read-only runtime evidence assembler | h1k.1 manually combined doctor/status/pressure/pulse/skills/MCP evidence. Existing stewardship and status code already gather overlapping slices. | Golden JSON for current status, pressure report, skills view, MCP audit, and stewardship snippets. Prove field names, ordering, warning semantics, and exit-code behavior unchanged. | `make doctor`; `make dev-sanity`; `python3 -m unittest tests.test_runtime_hotspot_units tests.test_pressure_report tests.test_pressure_visibility tests.test_mcp_server`. |
| 2 | Consolidate pressure advisory formatting across status, context, pulse, and stewardship | Pressure/offload warnings appear in several agent-facing surfaces. A shared formatter could reduce drift if golden text remains identical. | Golden text/JSON for `status`, generated context, pulse pressure event payload, and stewardship report. Prove no warning is added/removed/reordered unexpectedly. | `python3 -m unittest tests.test_pressure_report tests.test_pressure_visibility tests.test_runtime_hotspot_units`; `git diff --check`. |
| 3 | Unify MCP/CLI command contract metadata where it is duplicated | `manage.py`, `mcp_server.py`, wrappers, and docs expose overlapping command/help contracts. A shared declarative source may prevent drift. | Snapshot current `capabilities`, `robot-docs`, `robot-triage`, MCP tool schemas, and wrapper help before edits. Prove exact safe-first commands and next-actions stay equivalent except intentionally documented changes. | `python3 -m unittest tests.test_cli_units tests.test_mcp_server tests.test_cli_wrappers`; `make doctor`. |
| 4 | Split `skill_visibility.py` into visibility collection, lifecycle planning, audit, and text rendering modules | The file combines collection, policy, lifecycle actions, audit, compacting, and printing. Split only if tests/goldens make import paths and output stable. | Golden JSON for `skills`, `skill-audit`, `skill plan`, `skill sync --dry-run`, and `overlay activate --dry-run` where available. Prove public function imports used by CLI/MCP remain stable or are re-exported intentionally. | `python3 -m unittest tests.test_cli_units tests.test_cli_wrappers tests.test_mcp_server`; `python3 .env-manager/manage.py skills --issues-only --format json`. |
| 5 | Keep `scripts/box.py` lifecycle helpers factored by behavior, not by size | `box.py` is large, but current status/SSH performance fixes are already in place. Refactor only around a proven seam such as dry-run payloads or SSH validation reuse. | Golden JSON for `box list`, `box profiles`, `box status`, `box up --dry-run`, `box down --dry-run`; prove subprocess argv, state updates, dry-run markers, and inventory writes are unchanged. | `python3 -m unittest tests.test_box_refactor tests.test_box_lifecycle tests.test_operator_mcp_server`; dry-run CLI commands only. |

## Opportunity Matrix

Scores are planning estimates, not permission to edit. The future implementation Bead must replace them with measured LOC, duplication, and golden-output artifacts.

| Candidate | LOC saved | Confidence | Risk | Score | Decision |
|---|---:|---:|---:|---:|---|
| Pressure advisory formatter consolidation | 2 | 4 | 2 | 4.0 | Accept as future refactor candidate after goldens. |
| Runtime evidence assembler | 3 | 3 | 3 | 3.0 | Accept if h1k.5 mints evidence-packet work. |
| MCP/CLI command metadata source | 4 | 2 | 4 | 2.0 | Accept only with snapshot artifacts; high drift risk. |
| Skill visibility module split | 3 | 2 | 4 | 1.5 | Reject for now unless profiling proves it blocks work. |
| Broad `box.py` split by file size | 2 | 2 | 5 | 0.8 | Reject; size alone is not a behavior-preserving reason. |

## Measurement Packets For Future Beads

Every future profiling Bead should include this packet before code changes:

```text
scenario:
  name: <short command/path>
  command: <exact command>
  expected_output: <golden path or JSON keys>
  success_metric: p95 wall time, peak RSS, and top cumulative function
environment:
  fingerprint: tests/artifacts/perf/<run-id>/fingerprint.json
baseline:
  runs: 20
  output: tests/artifacts/perf/<run-id>/baseline.json
profile:
  cpu: tests/artifacts/perf/<run-id>/cprofile.pstats
  hotspot_table: tests/artifacts/perf/<run-id>/hotspot-table.md
hypotheses:
  ledger: tests/artifacts/perf/<run-id>/hypothesis-ledger.md
```

Every future refactor Bead should include this proof card before edits:

```text
equivalence_contract:
  inputs_covered: <commands/tests/goldens>
  ordering_preserved: yes/no with reason
  error_semantics: same variants, exit codes, and next_actions
  side_effects: same files, logs, markers, and state writes
  public_imports: same or intentionally re-exported
verification:
  golden_json_or_text: <path>
  tests: <exact commands>
  loc_delta: <path>
```

## Recommended h1k.5 Minting

Mint profiling/refactor implementation Beads only for:

1. Runtime evidence packet measurement plus assembler, if accepted by h1k.5.
2. Pulse reconciliation proof and timing packet, if the dirty pulse work is intended to land.
3. Pressure advisory formatter consolidation, but only after golden outputs are captured.

Do not mint broad module splits or box lifecycle performance rewrites from stale audit findings without fresh measurement.
