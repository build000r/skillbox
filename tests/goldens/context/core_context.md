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

## Ports

Single source of truth for declared ports. Run `python3 .env-manager/manage.py ports --format json` for the live registry.

| Port | Owner | Kind | Client | Profiles | Bind | Source |
|------|-------|------|--------|----------|------|--------|
| 3000 | SKILLBOX_WEB_PORT | env_surface | - | - | loopback | `.env:SKILLBOX_WEB_PORT` |
| 3210 | SKILLBOX_SWIMMERS_PORT | env_surface | - | - | loopback | `.env:SKILLBOX_SWIMMERS_PORT` |
| 3220 | SKILLBOX_DCG_MCP_PORT | env_surface | - | - | loopback | `.env:SKILLBOX_DCG_MCP_PORT` |
| 3221 | SKILLBOX_FWC_MCP_PORT | env_surface | - | - | loopback | `.env:SKILLBOX_FWC_MCP_PORT` |
| 3222 | SKILLBOX_CM_MCP_PORT | env_surface | - | - | loopback | `.env:SKILLBOX_CM_MCP_PORT` |
| 8000 | SKILLBOX_API_PORT | env_surface | - | - | loopback | `.env:SKILLBOX_API_PORT` |
| 8080 | SKILLBOX_INGRESS_PUBLIC_PORT | env_surface | - | - | loopback | `.env:SKILLBOX_INGRESS_PUBLIC_PORT` |
| 9080 | SKILLBOX_INGRESS_PRIVATE_PORT | env_surface | - | - | loopback | `.env:SKILLBOX_INGRESS_PRIVATE_PORT` |

- WARNING: service 'internal-env-manager' health check (unknown type) declares no port; not registered
- WARNING: service 'pulse' health check (path_exists) declares no port; not registered

## Installed Skills

- **default-skills**: (empty)

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
