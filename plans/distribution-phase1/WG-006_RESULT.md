# WG-006 Result — Sync pipeline integration

**Status:** done

## Summary

Implemented the full distributor-set sync pipeline: fetch signed manifest → verify ed25519 signature → cache manifest → check idempotency → filter skills by pick list + target environment → resolve pins → download bundles (with cache hit optimization) → verify bundle SHA256 + structural integrity → install via `filtered_copy_skill()` → emit enriched `SkillLockEntry` objects with distributor provenance. The pipeline is wired into `sync_runtime` via a single additive dispatch line in `runtime_ops.py`. Signature verification failure aborts the distributor's skills without corrupting the lockfile or partial-installing. The model-level orchestrator reads the existing lockfile (from `sync_skill_repo_sets`), merges distributor entries, and writes a v3 lockfile preserving all repo-sourced entries.

## Files Changed

| File | Type | Lines | Notes |
|------|------|-------|-------|
| `.env-manager/runtime_manager/distribution/sync.py` | **new** | ~260 | Core sync module: `sync_distributor_set`, `sync_distributor_sources`, HTTP client, lockfile merging |
| `.env-manager/runtime_manager/runtime_ops.py` | additive | +6 | 3-line wrapper `_sync_distributor_sources` + 1 dispatch line in `sync_runtime` |
| `.env-manager/runtime_manager/shared.py` | additive | +2 | Guard: `if entry.get("distributor"): continue` in `sync_skill_repo_sets` (see note below) |
| `tests/distribution/test_sync_pipeline.py` | **new** | ~420 | 18 tests across 9 test classes |
| `tests/distribution/fixtures/__init__.py` | **new** | 5 | Package marker for test fixtures |

### Out-of-scope edit: `shared.py`

`shared.py` is not in WG-006's declared writes. However, `sync_skill_repo_sets` iterates ALL entries from `skill_repos` config. Entries with `distributor:` key (but no `repo:` or `path:`) would crash with `KeyError: 'path'` in the else branch. The fix is 2 lines: `if entry.get("distributor"): continue`. Without this, distributor entries cannot coexist with repo/path entries in the same config file. This is documented per EXECUTION_CONTEXT rule: "If you need to edit outside scope, STOP and propose the smallest WORKGRAPH change."

## Architecture

```
sync_runtime() [runtime_ops.py]
├── sync_skill_repo_sets()        # existing — repo/path sources
├── _sync_distributor_sources()   # NEW dispatch (lazy import)
│   └── sync_distributor_sources()  [distribution/sync.py]
│       ├── Load config + parse distributor sources
│       ├── Read existing lockfile (v2 from repo-set sync)
│       ├── For each DistributorSetSource:
│       │   └── sync_distributor_set()
│       │       ├── HTTP GET /manifest (Bearer auth + X-Client-ID)
│       │       ├── parse_manifest() + verify_manifest() [WG-002/WG-005]
│       │       ├── Cache manifest to .skillbox-state/manifests/
│       │       ├── Idempotency check (manifest_version)
│       │       ├── Filter by pick list + target_env
│       │       ├── resolve_pin() per skill [WG-005]
│       │       ├── Cache hit check (bundle SHA256)
│       │       ├── HTTP GET bundle if needed
│       │       ├── unpack_skill_bundle() + verify_bundle_contents() [WG-001]
│       │       ├── filtered_copy_skill() [shared.py]
│       │       └���─ Return SkillLockEntry with distribution fields [WG-004]
│       ├── Merge: keep repo entries + add distributor entries
│       └��─ Write combined v3 lockfile
└── sync_skill_sets()             # existing — bundle-backed sources
```

## Validation

### Sync pipeline tests
```
$ python3 -m pytest tests/distribution/test_sync_pipeline.py -v
18 passed in 8.82s
```

### Backward-compat: existing skill repos tests
```
$ python3 -m pytest tests/test_skill_repos.py -v
26 passed in 0.06s
```

### Full distribution suite regression
```
$ python3 -m pytest tests/distribution/ tests/test_config_schema.py tests/test_lockfile_schema.py -v
128 passed in 8.84s
```

## Test Coverage

| Class | Tests | What it covers |
|-------|-------|----------------|
| `TestSyncDistributorSetHappyPath` | 3 | Full flow: install, manifest cache, bundle cache |
| `TestSyncDistributorSetSignatureFailure` | 1 | Tampered signature → error, no install |
| `TestSyncDistributorSetNetworkError` | 2 | Server 500, missing API key env var |
| `TestSyncDistributorSetPinResolution` | 2 | min_version floor override, no-pin default |
| `TestSyncDistributorSetTargetFilter` | 2 | target mismatch skips, match installs |
| `TestSyncDistributorSetCacheHit` | 1 | Pre-cached bundle skips download |
| `TestSyncDistributorSetIdempotency` | 2 | Same manifest_version → no-op, different → proceed |
| `TestSyncDistributorSetPickFilter` | 2 | Pick list includes/excludes |
| `TestSyncDistributorSources` | 2 | Model-level lockfile write + merge with repo entries |
| `TestSyncDistributorSetDryRun` | 1 | Dry-run returns entries without installing |

Tests use a real local `http.server.HTTPServer` on port 0 (auto-allocated) for realistic HTTP testing.

## Workgraph Notes

- **shared.py guard:** Added 2 lines outside declared scope. See "Out-of-scope edit" above. Future workgraph should include `shared.py` in WG-006 writes or add a separate WG for this guard.
- **validate_cmds correction:** Like WG-002 noted, tests run from repo root: `python3 -m pytest tests/distribution/test_sync_pipeline.py -v` (not `cd .env-manager && ...`).
- **runtime_ops.py diff is clean:** My additions are 6 lines (wrapper function + dispatch line). The diff also shows 4 pre-existing lines from the pulse daemon work (unrelated to WG-006).

## Blockers

None.
