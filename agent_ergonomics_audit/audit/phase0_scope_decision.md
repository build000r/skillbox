# Phase 0 Scope Decision

- Target: `/Users/b/repos/opensource/skillbox`
- Mode: `full`
- Branch policy: current branch `main`; no new branch.
- Workspace policy: in-tree at `agent_ergonomics_audit/`; no sibling workspace.
- Toolchain fallback: skill preflight requires Linux `flock` and `timeout`; this macOS run used direct Python and shell verification instead of installing host packages.
- Guardrails: preserve existing dirty worktree changes; avoid destructive runtime, Docker, provisioning, Tailscale, DigitalOcean, or sync/up/down side effects.
- Primary surface for this pass: `.env-manager/manage.py`, the runtime manager CLI used by Makefile, MCP, and wrapper scripts.
