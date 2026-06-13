# skillbox

> Auto-generated from the runtime graph. Do not edit manually.
> Regenerate: `make context` or `make runtime-sync`.

You are inside a skillbox workspace container.

## Environment


## Tooling Guidance

- Start agent navigation with `python3 .env-manager/manage.py capabilities --json`, then `python3 .env-manager/manage.py next --format json`.
- GitHub: use `gh-axi` for GitHub operations when available; fall back to `gh` only when `gh-axi` cannot satisfy the task.

## Pressure And Offload Policy

<PRESSURE-ADVISORY-NORMALIZED>

## Repos

| ID | Path | Kind | Project | Command Lanes |
|----|------|------|---------|---------------|
| skillbox-self | `/workspace` | repo | - | - |
| managed-repos | `/workspace/repos` | workspace-root | - | - |

## Services

- **internal-env-manager** (orchestration, core) — orchestration services are status-only
- **pulse** (daemon, core)
  - Start: `make runtime-up SERVICE=pulse`
  - Stop: `make runtime-down SERVICE=pulse`
  - Logs: `make runtime-logs SERVICE=pulse`

## Installed Skills

- **default-skills**: beads-br, beads-bv, beads-workflow, codebase-audit, divide-and-conquer, git-stash-janitor, lube, mmdx, no-ragrets, project-status-mmdx, sbp, skill-issue, smart, ui-fresh-eyes

## Logs

| ID | Path |
|----|------|
| runtime | `/workspace/logs/runtime` |
| repos | `/workspace/logs/repos` |

## Quick Reference

```bash
python3 .env-manager/manage.py capabilities --json
python3 .env-manager/manage.py next --format json
make dev-sanity
make runtime-status
make runtime-sync
make runtime-up SERVICE=<id>
make runtime-down SERVICE=<id>
make runtime-logs SERVICE=<id>
```
