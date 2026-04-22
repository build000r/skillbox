# WG-001 Result — Bundle format primitives

## Status

done

## Summary

Implemented `pack_skill_bundle`, `unpack_skill_bundle`, and `verify_bundle_contents` as pure data-layer primitives in `.env-manager/runtime_manager/distribution/bundle.py`. The tree SHA256 algorithm mirrors `shared.py`'s `tree_hash()` format (`path\0sha256\n`) for consistency, but excludes `.skill-meta/` from the content hash to avoid circular dependency with the manifest. Tarballs are fully deterministic (fixed mtime=0, uid=0, gid=0, sorted entries, gzip mtime=0) — identical repacks produce byte-identical bundles. Created `distribution/__init__.py` as the package marker.

## Files Changed

| Path | Action |
|------|--------|
| `.env-manager/runtime_manager/distribution/__init__.py` | created (package docstring) |
| `.env-manager/runtime_manager/distribution/bundle.py` | created (246 lines — bundle format primitives) |
| `tests/distribution/test_bundle.py` | created (24 tests across 6 test classes) |

## Validation

```
$ python -m pytest tests/distribution/test_bundle.py -v
24 passed in 0.10s
```

```
$ cd .env-manager && python -c "from runtime_manager.distribution.bundle import pack_skill_bundle, unpack_skill_bundle; print('ok')"
ok
```

Signing tests (WG-002) confirmed unaffected: 27 passed.

## Test coverage

| Category | Tests |
|----------|-------|
| Round-trip (pack → unpack → verify) | content preservation, tree SHA determinism, byte-identical repacks, custom name/tags, output_dir override, preserves existing .skill-meta files |
| Unpack errors | missing manifest.json, non-gzip input, plain tar, nonexistent file, malformed JSON, missing required fields |
| Verify errors | file hash mismatch, missing file, tree SHA mismatch from extra file |
| Manifest serialization | to_dict/from_dict round-trip, missing fields, malformed files array |
| Tree SHA | .skill-meta exclusion, determinism, different-content-different-SHA |
| Edge cases | nonexistent source dir, empty skill dir |

## Workgraph Notes

- The validate command in WORKGRAPH.md runs from `.env-manager/` (`cd .env-manager && python -m pytest tests/distribution/test_bundle.py`), but the test file is at repo root `tests/distribution/test_bundle.py`. The correct invocation is `cd <repo> && python -m pytest tests/distribution/test_bundle.py -v`. Same applies to WG-002's test path.
- `distribution/__init__.py` was not yet present despite `signing.py` already existing from WG-002 — created as part of this node.

## Blockers

None.
