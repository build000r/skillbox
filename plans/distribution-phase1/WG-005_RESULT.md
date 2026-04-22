# WG-005 Result — Per-client manifest schema + pin resolver

## Status

done

## Summary

Implemented `manifest.py` (per-client manifest parser, signature verifier, target filter) and `pin_resolver.py` (pure-function pin resolution with `pinned_by` tagging). The manifest parser validates all required fields, rejects unsupported schema versions, and preserves the raw dict for signature verification delegation to `signing.verify_manifest_signature`. The pin resolver implements the exact formula from the plan with exhaustive coverage of the 5-case test matrix plus 6 edge cases. All dataclasses are frozen.

## Files Changed

| Path | Action |
|------|--------|
| `.env-manager/runtime_manager/distribution/manifest.py` | created — `parse_manifest`, `verify_manifest`, `filter_skills_for_target`, `ClientManifest`, `ClientManifestSkill`, `ManifestSchemaError` |
| `.env-manager/runtime_manager/distribution/pin_resolver.py` | created — `resolve_pin`, `PinResolution` |
| `tests/distribution/test_manifest.py` | created — 33 tests across 4 classes |
| `tests/distribution/test_pin_resolver.py` | created — 17 tests across 3 classes |

## Validation

```
$ cd <repo> && python3 -m pytest tests/distribution/test_manifest.py tests/distribution/test_pin_resolver.py -v
50 passed in 0.10s
```

Full distribution suite (101 tests) confirmed green — no regressions against WG-001, WG-002.

## Test coverage

### test_manifest.py (33 tests)

| Class | Tests |
|-------|-------|
| TestParseManifest | valid manifest, multiple skills, optional fields present/absent, raw dict preserved, invalid JSON, not-an-object, unsupported schema version, missing each required top-level field, missing/invalid skills list, skill-level missing required fields (name, version, sha256, download_url, targets), targets not a list, skill not an object, empty skills list, boolean rejected as integer |
| TestVerifyManifest | valid signature passes, tampered manifest fails, wrong key fails, missing signature fails |
| TestFilterSkillsForTarget | filters by target, empty targets matches nothing, unknown target returns empty, all match, empty manifest |

### test_pin_resolver.py (17 tests)

| Class | Tests |
|-------|-------|
| TestPinResolutionMatrix (5 required cases) | no-pin/no-floor → v8 recommendation; client=6/dist=8 → v6 client; client=6/min=7 → v7 floor+reason; client=9/dist=8 → v8 recommendation (capped); no-pin/min=7 → v8 recommendation |
| TestPinResolutionEdgeCases (10 cases) | pin==recommended, pin at floor, pin one above floor, floor==recommended, floor>recommended, pin=0, pin=0 with floor, floor reason None, version 1, all three equal |
| TestPinResolutionDataclass | frozen, default reason None |

## Workgraph Notes

None — clean dependency on WG-002 signing primitives. No out-of-scope edits needed.

## Blockers

None.
