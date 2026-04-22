Status: done

Summary:
Implemented lockfile schema enrichment in a new distribution module without touching `shared.py`. Added typed dataclasses for distributor manifest metadata and per-skill lock entries, including optional distribution fields (`source`, `distributor_id`, `version`, `bundle_sha256`, `pinned_by`, `pin_reason`). Added emit/parse helpers with legacy forward-compat so lockfiles missing new fields still parse and default skill `source` to `repo`.

Files Changed:
- .env-manager/runtime_manager/distribution/lockfile.py
- tests/test_lockfile_schema.py

Validation:
- PASS: `python3 -m pytest tests/test_lockfile_schema.py -v`
- PASS: `cd .env-manager && python -m pytest ../tests/test_lockfile_schema.py -v`

Workgraph Notes:
- This node intentionally did not modify `shared.py` per WG-004 constraints.
- `parse_lockfile()` is backward-compatible with legacy lockfiles and adds normalized `source="repo"` when absent.
- `emit_lockfile()` currently always emits `distributor_manifests` (empty object when none supplied), which is additive and version-3 friendly.

Blockers:
- none
