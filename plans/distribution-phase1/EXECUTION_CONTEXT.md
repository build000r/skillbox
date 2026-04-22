# Execution Context — Distribution Phase 1

## User's underlying ask

Implement Phase 1 of the skillbox multi-client skill distribution architecture (the consensus merged design from `DUELING_WIZARDS_REPORT_R2.md`) — the minimum code needed so a client can sync signed skill bundles from a distributor via an authenticated HTTP feed, with sync-time-only content delivery and the lockfile as canonical truth.

## Repo + branch

- Repo: `<repo>`
- Branch: `main`
- HEAD at execution start: check via `git rev-parse HEAD`
- Python package: `.env-manager/runtime_manager/`

## Workgraph

- File: `<repo>/plans/distribution-phase1/WORKGRAPH.md` (durable, lives with plan)
- Invocation run directory: `<repo>/plans/distribution-phase1/` (result artifacts land alongside the graph; the overlay-backed plan index in `skillbox-config` now tracks this slice by name)
- Source plan: `plan.md` in the same directory

## Wave layout

```
Wave 1 (independent writes, 4 nodes, parallel):
  WG-001 bundle_format      (distribution/bundle.py)
  WG-002 signing            (distribution/signing.py)
  WG-003 config_schema      (shared_distribution.py)
  WG-004 lockfile_schema    (distribution/lockfile.py)

Wave 2 (1 node):
  WG-005 manifest_schema    (needs: WG-002)

Wave 3 (1 node):
  WG-006 sync_pipeline      (needs: all above)

Wave 4 (parallel, 2 nodes):
  WG-007 doctor_checks      (needs: WG-006)
  WG-008 auth_surface       (needs: WG-003, WG-006)

Wave 5 (1 node):
  WG-009 integration_smoke  (needs: WG-006, WG-007, WG-008)

Post: /crap hardening gate → target FINAL_SCORE ≤ 30
Post: /smart loop reflection
```

## Global constraints (binding for every worker)

1. **Edit only files in your node's `writes` list.** If you need to edit outside scope, STOP and propose the smallest WORKGRAPH change.
2. **No commits.** Only the final integration review wave commits.
3. **No cross-repo edits.** Single repo (skillbox) only.
4. **Preserve backward compatibility** for existing `repo:` and `path:` source kinds. Existing configs must continue to parse and sync.
5. **Prefer new modules** (`.env-manager/runtime_manager/distribution/*.py`, `.env-manager/runtime_manager/shared_distribution.py`) over growing the already-large `shared.py` (3781 lines) and `runtime_ops.py` (3595 lines).
6. **Use the `cryptography` Python library** for ed25519 (add to requirements if not present). Do not roll your own crypto.
7. **Deterministic outputs.** Bundle tar files, canonical JSON signing, file ordering — all must be reproducible.
8. **Run `validate_cmds` before declaring done.** Report pass/fail explicitly in `WG-*_RESULT.md`.

## Validation posture

- Python tests: `pytest` (likely already in project deps)
- Test layout: `.env-manager/tests/` or `<repo>/tests/` — workers should use whichever pattern the repo already uses
- Smoke: `make doctor`, `make runtime-sync` at wave boundaries
- Type check: if `mypy` or `pyright` is configured, run it

## Known risk gates

- `shared.py` is enormous (3781 lines) — avoid touching it directly; use `shared_distribution.py` for new types
- `runtime_ops.py` is enormous (3595 lines) — WG-006 will add a dispatcher branch only; do not refactor the whole file
- `validation.py` (1747 lines) — WG-007 will add new check functions only
- The MCP server (`mcp_server.py`, 1116 lines) does NOT need changes for Phase 1 — distribution is sync-time only, not runtime
- Existing lockfile format must remain parseable by old clients (additive fields only)

## Artifact contract

Every worker writes `WG-<NNN>_RESULT.md` to `<repo>/plans/distribution-phase1/` with:
- Status: `done` | `blocked` | `needs_rework`
- Summary (one paragraph)
- Files changed (list with paths)
- Validation (command + pass/fail + notes)
- Workgraph notes (any graph update suggestions)
- Blockers (only if blocked or needs_rework)

## Non-goals (reject if tempted)

- Runtime MCP content fetch — Round 1 categorically rejected, Round 2 preserved the rejection
- SQLite as state store — jsm's SQLite was corrupted on this exact machine; lockfile is the only acceptable state store
- Content overlays at session time — breaks `validate_skill_repo_sets()` invariant by design
- jsm federation / jsm API coupling
- Token exchange, device binding, background auto-sync, CAS, rings, laptop CLI — all deferred to Phase 2+
- Distributor-side publishing tooling (skillbox-dist CLI) — this Phase 1 implements the CLIENT side only
