# WG-002 Result — ed25519 signing primitives

**Status:** done

## Summary

Implemented all ed25519 signing primitives as a pure library module with no I/O beyond test fixtures. The module provides: `load_public_key` (parses `ed25519:<base64>` config format), `sign_manifest` / `verify_manifest_signature` (inline signature over canonicalized JSON with `signature` field excluded), `sign_detached` / `verify_detached_signature` (for bundle `.skill-meta/signature.json`), and `public_key_to_config_str` (serialization helper). All functions use the `cryptography` library's Ed25519 implementation. Custom exceptions `SignatureVerificationError` and `KeyFormatError` provide clear error messages for downstream consumers.

## Files Changed

- `.env-manager/runtime_manager/distribution/signing.py` (new — 120 lines)
- `tests/distribution/test_signing.py` (new — 27 tests across 6 test classes)

## Validation

```
$ python -m pytest tests/distribution/test_signing.py -v
27 passed in 0.21s
```

Note: The workgraph validate command says `cd .env-manager && python -m pytest tests/distribution/...` but the `tests/` directory is at repo root, not inside `.env-manager/`. Correct invocation is from repo root: `python -m pytest tests/distribution/test_signing.py -v`. Other swarm workers should use the same pattern.

Pre-existing test failure in `test_runtime_manager.py::test_default_skill_repos_config_matches_hardened_shared_pack` confirmed unrelated (fails on clean `main` HEAD without any of my changes).

## Test Coverage

- **TestLoadPublicKey** (7 tests): valid key, missing prefix, empty string, non-string input, invalid base64, wrong key length, wrong algorithm prefix
- **TestManifestSignAndVerify** (9 tests): round-trip, invalid signature, wrong key, missing field, malformed base64, field ordering independence, extra field injection detection, input non-mutation, existing signature replacement
- **TestDetachedSignature** (7 tests): round-trip, invalid signature, wrong algorithm, missing field, wrong key, tampered payload, malformed base64
- **TestCanonicalize** (3 tests): sorted keys, no whitespace, deterministic
- **TestPublicKeyRoundTrip** (1 test): config string serialize/deserialize

## Workgraph Notes

- `validate_cmds` in the workgraph should use repo-root-relative paths: `python -m pytest tests/distribution/test_signing.py -v` (not `cd .env-manager && ...`). This applies to all nodes.
- WG-001 owns `distribution/__init__.py`; I did not create it. If WG-005 (manifest_schema) needs to import from `signing.py`, it can do so directly: `from runtime_manager.distribution.signing import ...` (works with the sys.path setup used by existing tests).

## Blockers

None.
