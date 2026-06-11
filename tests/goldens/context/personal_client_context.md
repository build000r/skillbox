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
