# skillbox

> Auto-generated from the runtime graph. Do not edit manually.
> Regenerate: `make context` or `make runtime-sync`.

You are inside a skillbox workspace container.

## Environment


## Tooling Guidance

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

- **default-skills**: ask-cascade, audit-plans, build-vs-clone, cli-ergonomics, commit, crap, describe, dev-sanity, divide-and-conquer, domain-planner, domain-reviewer, domain-scaffolder, mutate, oss-doc-audit, reproduce, skill-issue, skillbox-operator

## Logs

| ID | Path |
|----|------|
| runtime | `/workspace/logs/runtime` |
| repos | `/workspace/logs/repos` |

## Quick Reference

```bash
make dev-sanity
make runtime-status
make runtime-sync
make runtime-up SERVICE=<id>
make runtime-down SERVICE=<id>
make runtime-logs SERVICE=<id>
```
