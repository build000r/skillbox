# Phase 0 Scope Decision

Date: 2026-06-25
Target repo: `/srv/skillbox/repos/opensource/skillbox`
Audit workspace: `/srv/skillbox/repos/opensource/skillbox/agent_ergonomics_audit`
Branch: `main`
Starting SHA: `10b06f0f9b63f789671eb27e7c463cd6bfaeb45c`
Mode: `full`

## Toolchain

Preflight passed with optional warnings:

- Present: git, jq, flock, node, awk, find, sed, timeout, target git repo, Beads/BV, CASS.
- Missing optional helpers: `/agent-mail`, `/multi-model-triangulation`, shellcheck.
- Generic binary discovery did not detect a single target binary because Skillbox intentionally exposes several entrypoints.

Manual override: inventory and scoring cover the documented multi-entry surfaces instead of one packaged binary.

## Scope

Primary surfaces:

- `python3 .env-manager/manage.py capabilities/next/graph/explain/search/snap`
- Runtime command registry and MCP mirror metadata for the brain commands.
- `scripts/sbp` wrapper capabilities, robot docs, triage, recalibrate, skill verbs.
- `scripts/04-reconcile.py` capabilities/render/doctor/robot docs/robot triage.
- `scripts/box.py` capabilities/profiles/list/status/up/down/robot docs/robot triage and MCP operator equivalents.

Guardrails:

- Do not run destructive box commands except dry-run previews.
- Do not start services, provision droplets, destroy boxes, or mutate secrets.
- Preserve existing local/generated state and unrelated dirty worktree files.
- Do not include `.env`, `.env.box`, `workspace/secrets/`, or local client overlays.
- Use focused tests for touched brain code, then run the bead validation commands.

## Subagent Review

Three read-only explorers independently reviewed:

- Runtime brain CLI and command registry.
- `scripts/sbp` wrapper and SBP-related runtime surfaces.
- `scripts/04-reconcile.py`, `scripts/box.py`, and operator MCP mirrors.

Their findings are synthesized in `scorecard_pass_1.md`, `recommendations.jsonl`, and `HANDOFF.md`.
