# Skillbox Reality Check - 2026-05-28

Scope: planning-only status pass for `skillbox-portfolio-reality-idea-plan-h1k.1`.
No product or runtime mutations were performed.
This is dated evidence from 2026-05-28; later push-readiness checks should be
treated as the current source of truth.

## Evidence Read

- `AGENTS.md`, `README.md`, `docs/VISION.md`, `docs/ROADMAP.md`, and `docs/shared-jam.md`
- `workspace/runtime.yaml`, `Makefile`, runtime manager CLI help, pulse status, pressure report, box inventory, skill/MCP audit surfaces
- Current Beads: `skillbox-portfolio-reality-idea-plan-h1k.1` through `.5`, parent `skillbox-portfolio-reality-idea-plan-h1k`, and `skillbox-smart-20260518-validation-trust-gkn`

## GREEN - Working And Proof-Backed

- Runtime contract exists and resolves: `python3 .env-manager/manage.py --help` exposes render/sync/doctor/status/pressure/skills/MCP/focus/session/worker/fleet commands, and `python3 .env-manager/manage.py render --profile core --format json` resolved the v2 runtime graph.
- Outer validation is currently passing with a warning boundary: `make doctor` reported 13 passed, 0 warnings, 0 failed; `make dev-sanity` reported 23 passed, 1 warning, 0 failed.
- Read-only pressure visibility works: `python3 .env-manager/manage.py pressure-report --format json` returned `mode: read_only`, `mutates: false`, critical local disk pressure, protected no-touch buckets, and remote worker guidance.
- Pulse is real in this checkout: `python3 .env-manager/pulse.py status` reported a running daemon, cycle/heal counts, service states, failed check visibility, and pressure/offload warnings.
- Fleet inventory surfaces are real: `python3 scripts/box.py list --format json` returned managed box records, and `python3 scripts/box.py profiles --format json` returned DigitalOcean dev profiles.
- Effective skill visibility works without an active client: `python3 .env-manager/manage.py skills --profile local-all --cwd <repo> --issues-only --format json` reported no broken or missing cwd-required skills.

## YELLOW - Bead-Covered Gaps

- Broad validation trust remains covered by `skillbox-smart-20260518-validation-trust-gkn`. Current cheap proof is better than the older note in that Bead for `make doctor` and `make dev-sanity`, but the broad unittest lane and disk-pressure-safe proof strategy were not rerun in this planning pass.
- Disk pressure was a real gate covered by `skillbox-smart-20260518-validation-trust-gkn`: this pass observed 1.6 GiB free and the pressure report marked it critical.
- Portfolio planning follow-ups are covered by `skillbox-portfolio-reality-idea-plan-h1k.2`, `.3`, `.4`, and `.5`; they should use this artifact as input and must not implement product/code changes during planning.

## RED - Uncovered Gaps

- The documented `personal` client path is not active in this checkout. `python3 .env-manager/manage.py skills --client personal ...` failed with `Unknown runtime client(s): personal`, and the rendered graph reported `active_clients: []`. No direct implementation Bead currently covers restoring or documenting that local-client expectation.
- MCP parity has an uncovered drift item. `python3 .env-manager/manage.py mcp-audit --cwd <repo> --format json` found valid configs, but Claude-only servers `cm`, `dcg`, and `fwc` are not mirrored in Codex TOML or removed as obsolete. No direct implementation Bead currently covers that parity decision.

## GRAY - Not Proven In This Pass

- Runtime-starting and infrastructure-mutating paths were intentionally not executed: `make up`, `make up-surfaces`, `make runtime-up`, `make box-up`, `operator_provision`, destructive teardown, and remote SSH execution.
- Worker broker launch was not executed because `worker-submit` writes broker state and requires an installed runtime command for successful execution; only the CLI contract was checked.
- API and web surfaces remain inspection stubs per README limitations, not a full product UI.
- Hosted distributor service, standalone laptop CLI, background update checks, and short-lived token exchange are future work per README limitations.

## Next Planning Moves

1. Close or update `skillbox-smart-20260518-validation-trust-gkn` against current proof, including a disk-safe broad validation lane.
2. In `skillbox-portfolio-reality-idea-plan-h1k.2` through `.5`, decide whether to mint implementation Beads for the active-client gap and MCP parity gap.
3. Keep infrastructure commands behind dry-run or explicit operator approval until the disk-pressure gate and dirty-worktree state are resolved.
