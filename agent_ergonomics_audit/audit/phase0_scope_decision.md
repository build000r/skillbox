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

---

# Phase 0 Scope Decision — Pass 2

Date: 2026-08-14
Target repo: `/Users/b/repos/opensource/skillbox` (host checkout; pass 1 ran in-container at /srv)
Audit workspace: `/Users/b/repos/opensource/skillbox/agent_ergonomics_audit`
Branch: `main` (no new branch — Axiom 1)
Starting SHA: (recorded in manifest at pass start)
Mode: `full`

## Preflight deltas vs pass 1

- `flock` missing on macOS host (BSD). Accepted fallback: single-applier mode — no
  concurrent Phase 5 writers, so flock-based flip_applied locking is unnecessary.
- /agent-mail and /multi-model-triangulation still absent → peer-claude triangulation.

## Scope (pass 2)

Context since pass 1 (SHA 10b06f0 → HEAD): inbox MCP deprecated + frozen (canonical
path = CLI + skills); sbp gained `help --human` operator console; deferred beads
.6/.8/.9 were closed by intervening work and need verification-at-HEAD.

1. Verify closed beads hold at HEAD: 086q.8 (sbp JSON/mutation contract),
   086q.9 (box.py safety/JSON errors), 086q.6 (unified error envelope).
2. Re-inventory + re-score the five pass-1 surface families at HEAD.
3. Apply focus: box.py robot JSON + mutation gating parity with operator MCP —
   the precondition for open bead skillbox-mcp-deprecation-epic-vniq.4.
4. Doctor-surface coherence read (sbp doctor / make doctor / manage.py doctor /
   04-reconcile.py doctor) as seed input for the follow-on doctor-mode pass.

## Guardrails (carried + new)

- operator_mcp_server.py is intentionally standalone; tests mock by module
  namespace; only pure helpers may hoist to shared.py. Do not refactor it.
- MCP ABI is FROZEN: never add MCP tools; registry mcp_tool set must stay honest.
- No new branches; no sibling workspaces; docs/status planning artifacts stay untracked.
- Broad raw-host `unittest discover` is a known-bad signal on macOS; gate on
  per-file suites + containerized make self-test.
