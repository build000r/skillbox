Status: done

Summary:
Implemented WG-008 auth/status surface with minimal hooks. Added a new distribution status adapter module that computes distributor rows from configured `distributors` blocks + lockfile/cached manifest state (best-effort, no network). `runtime_status()` now includes a `distributors` field, and `generate_context_markdown()` now conditionally renders `## Connected Distributors` when distribution config is present. Legacy configs without `distributors:` remain unchanged in context output.

Files Changed:
- .env-manager/runtime_manager/distribution/status.py (new)
- tests/test_auth_surface.py (new)
- .env-manager/runtime_manager/runtime_ops.py (additive hook only)
- .env-manager/runtime_manager/context_rendering.py (additive hook only)

Validation:
- PASS: `cd <repo> && python3 -m pytest tests/test_auth_surface.py -v`
- PASS: `cd <repo> && python3 -m pytest tests/distribution/ tests/test_config_schema.py tests/test_lockfile_schema.py tests/test_skill_repos.py -v`
- NOTE: requested smoke cmd `python3 .env-manager/manage.py runtime-status` is not a valid manage subcommand in this repo (CLI uses `status`).
- PASS (equivalent): `cd <repo> && python3 .env-manager/manage.py status 2>&1 | head -40`
- PASS (json shape check): `cd <repo> && python3 .env-manager/manage.py status --format json` includes `"distributors": []` in current legacy config.

Workgraph Notes:
- `auth_probe_result` is cache-only and non-probing by design for Phase 1. This implementation reads optional cached probe files under `.skillbox-state/manifests/` and otherwise reports `unknown`.
- MCP `skillbox_status` required no code change because it already forwards `manage.py status --format json`; the new `runtime_status()` field flows through automatically.
- Runtime/context hooks are intentionally tiny and isolated.

Blockers:
- none
