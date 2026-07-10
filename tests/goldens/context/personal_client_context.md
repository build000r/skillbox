# skillbox

> Auto-generated from the runtime graph. Do not edit manually.
> Regenerate: `make context CLIENT=personal` or `make runtime-sync CLIENT=personal`.

You are inside a skillbox workspace container.

## Environment

- Client: **personal**
- Default CWD: `/monoserver`
- Skill context: `$SKILLBOX_CLIENT_CONTEXT` → `/workspace/workspace/clients/personal/context.yaml`

## Tooling Guidance

- Start agent navigation with `python3 .env-manager/manage.py capabilities --json`, then `python3 .env-manager/manage.py next --format json`.
- GitHub: use `gh-axi` for GitHub operations when available; fall back to `gh` only when `gh-axi` cannot satisfy the task.

## Pressure And Offload Policy

<PRESSURE-ADVISORY-NORMALIZED>

## Repos

| ID | Path | Kind | Project | Command Lanes |
|----|------|------|---------|---------------|
| personal-root | `/monoserver` | repo-root | - | - |
| recipe-ios | `/monoserver/recipe-ios` | repo | ios | build, test, sim, device-local, device-fixtures, device-prod, archive, upload, screenshots |

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

## Quick Reference

```bash
python3 .env-manager/manage.py capabilities --json
python3 .env-manager/manage.py next --format json
make dev-sanity CLIENT=personal
make runtime-status CLIENT=personal
make runtime-sync CLIENT=personal
make runtime-up CLIENT=personal SERVICE=<id>
make runtime-down CLIENT=personal SERVICE=<id>
make runtime-logs CLIENT=personal SERVICE=<id>
```
