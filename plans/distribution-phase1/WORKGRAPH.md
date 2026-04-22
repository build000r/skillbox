# WORKGRAPH — Distribution Phase 1 (skillbox multi-client skill distribution)

Execution handoff for divide-and-conquer NTM swarm (cc + cod). Each node is a self-contained brief an agent can pick up without re-reading the plan. Dependency edges are explicit; `writes` globs ensure no parallel write overlap within a wave.

Repo root: `<repo>`
Python package: `.env-manager/runtime_manager/`
Entrypoint: `.env-manager/manage.py`

## Machine-readable node graph

```json
{
  "nodes": [
    {
      "id": "WG-001",
      "title": "Bundle format primitives",
      "concern": "bundle-format",
      "repo": "skillbox",
      "depends_on": [],
      "writes": [".env-manager/runtime_manager/distribution/__init__.py", ".env-manager/runtime_manager/distribution/bundle.py", "tests/distribution/test_bundle.py"],
      "done_when": ["pack_skill_bundle and unpack_skill_bundle implemented with deterministic tree SHA", "verify_bundle_contents raises on file-hash mismatch", "unit tests cover round-trip, missing manifest, file mismatch, non-gzip"],
      "validate_cmds": ["cd <repo> && python3 -m pytest tests/distribution/test_bundle.py -v"],
      "risk_gate": "none",
      "status": "done"
    },
    {
      "id": "WG-002",
      "title": "ed25519 signing primitives",
      "concern": "signing",
      "repo": "skillbox",
      "depends_on": [],
      "writes": [".env-manager/runtime_manager/distribution/signing.py", "tests/distribution/test_signing.py"],
      "done_when": ["load_public_key parses ed25519:base64 format", "verify_manifest_signature and verify_detached_signature implemented", "sign_manifest implemented for tests", "unit tests cover valid, invalid, malformed, field ordering"],
      "validate_cmds": ["cd <repo> && python3 -m pytest tests/distribution/test_signing.py -v"],
      "risk_gate": "none",
      "status": "done"
    },
    {
      "id": "WG-003",
      "title": "Config schema (distributor-set + distributors section)",
      "concern": "config-schema",
      "repo": "skillbox",
      "depends_on": [],
      "writes": [".env-manager/runtime_manager/shared_distribution.py", "tests/test_config_schema.py"],
      "done_when": ["DistributorConfig and DistributorSetSource dataclasses defined", "load_skill_repos_config parses new sections additively", "validate_distributor_refs raises on dangling id", "existing repo:/path: tests still pass"],
      "validate_cmds": ["cd <repo> && python3 -m pytest tests/test_config_schema.py -v", "cd <repo> && PYTHONPATH=.env-manager python3 -c 'from runtime_manager.shared import load_skill_repos_config; print(\"ok\")'"],
      "risk_gate": "backward-compat with existing shared.py callers",
      "status": "done"
    },
    {
      "id": "WG-004",
      "title": "Lockfile schema enrichment",
      "concern": "lockfile-schema",
      "repo": "skillbox",
      "depends_on": [],
      "writes": [".env-manager/runtime_manager/distribution/lockfile.py", "tests/test_lockfile_schema.py"],
      "done_when": ["DistributorManifestLockEntry dataclass defined", "extended SkillLockEntry with optional distribution fields", "emit_lockfile and parse_lockfile handle new shape", "legacy lockfile without distribution fields parses cleanly"],
      "validate_cmds": ["cd <repo> && python3 -m pytest tests/test_lockfile_schema.py -v"],
      "risk_gate": "legacy lockfile forward-compat",
      "status": "done"
    },
    {
      "id": "WG-005",
      "title": "Per-client manifest schema + pin resolver",
      "concern": "manifest-schema",
      "repo": "skillbox",
      "depends_on": ["WG-002"],
      "writes": [".env-manager/runtime_manager/distribution/manifest.py", ".env-manager/runtime_manager/distribution/pin_resolver.py", "tests/distribution/test_manifest.py", "tests/distribution/test_pin_resolver.py"],
      "done_when": ["parse_manifest validates schema", "verify_manifest delegates to signing", "resolve_pin passes the full pin-resolution matrix including all combinations of client pin set/unset and manifest min_version set/unset", "filter_skills_for_target drops non-matching target skills"],
      "validate_cmds": ["cd <repo> && python3 -m pytest tests/distribution/test_manifest.py tests/distribution/test_pin_resolver.py -v"],
      "risk_gate": "none",
      "status": "done"
    },
    {
      "id": "WG-006",
      "title": "Sync pipeline integration",
      "concern": "sync-pipeline",
      "repo": "skillbox",
      "depends_on": ["WG-001", "WG-002", "WG-003", "WG-004", "WG-005"],
      "writes": [".env-manager/runtime_manager/distribution/sync.py", "tests/distribution/test_sync_pipeline.py", "tests/distribution/fixtures/"],
      "done_when": ["sync_distributor_set returns lock entries for successful installs", "runtime_ops dispatcher routes DistributorSetSource without breaking repo:/path:", "signature verification failure aborts cleanly without corrupting lockfile", "pin resolution integrated", "target filter applied", "backward-compat smoke passes"],
      "validate_cmds": ["cd <repo> && python3 -m pytest tests/distribution/test_sync_pipeline.py -v", "cd <repo> && python3 -m pytest tests/test_skill_repos.py -v"],
      "risk_gate": "runtime_ops.py dispatcher must preserve existing source-kind behavior",
      "status": "done"
    },
    {
      "id": "WG-007",
      "title": "Doctor distribution checks",
      "concern": "doctor-checks",
      "repo": "skillbox",
      "depends_on": ["WG-006"],
      "writes": [".env-manager/runtime_manager/distribution/doctor.py", "tests/test_doctor_distribution.py"],
      "done_when": ["five check functions implemented and registered", "distributor_manifest_signature is HARD FAIL on tamper", "other checks are warnings for offline-friendly operation", "make doctor integration test passes with + without distributors configured"],
      "validate_cmds": ["cd <repo> && python3 -m pytest tests/test_doctor_distribution.py -v"],
      "risk_gate": "validation.py must stay backward-compatible",
      "status": "done"
    },
    {
      "id": "WG-008",
      "title": "Auth surface in status and context",
      "concern": "auth-surface",
      "repo": "skillbox",
      "depends_on": ["WG-003", "WG-006"],
      "writes": [".env-manager/runtime_manager/distribution/status.py", "tests/test_auth_surface.py"],
      "done_when": ["runtime_status includes distributors section when configured", "generated CLAUDE.md adds Connected Distributors section", "no section rendered for legacy config"],
      "validate_cmds": ["cd <repo> && python3 -m pytest tests/test_auth_surface.py -v", "cd <repo> && python3 .env-manager/manage.py status --format json"],
      "risk_gate": "none",
      "status": "done"
    },
    {
      "id": "WG-009",
      "title": "End-to-end integration smoke test",
      "concern": "integration-smoke",
      "repo": "skillbox",
      "depends_on": ["WG-006", "WG-007", "WG-008"],
      "writes": ["tests/distribution/test_end_to_end.py", "tests/distribution/fixture_server.py"],
      "done_when": ["fixture HTTP server serves signed manifest + bundle", "full pipeline runs config to sync to install to lockfile to doctor", "test completes in under 30s without real network", "no artifacts outside temp dir"],
      "validate_cmds": ["cd <repo> && python3 -m pytest tests/distribution/test_end_to_end.py -v"],
      "risk_gate": "none",
      "status": "done"
    }
  ]
}
```

---

## Node: `bundle_format` — Define bundle packing/unpacking primitives

**Status:** todo
**Depends on:** (none)
**Writes:** `.env-manager/runtime_manager/distribution/__init__.py`, `.env-manager/runtime_manager/distribution/bundle.py`, `tests/distribution/test_bundle.py`
**Context:** Phase 1 introduces a `.skillbundle.tar.gz` packaging unit for skills distributed by a trusted distributor. This node implements only the *format* primitives — pack, unpack, structural validation. Signing is a separate node.
**Contract excerpt:**
- Bundle layout: `SKILL.md`, `references/`, `.skill-meta/{manifest.json, signature.json, changelog.md, compatibility.json}`
- `manifest.json` inside bundle: `{name, version:int, tree_sha256, min_skillbox_version, tags[], files:[{path, sha256}]}`
- Pack: given a directory, produce `.skillbundle.tar.gz` with deterministic file ordering for reproducible hashes
- Unpack: extract to a temp dir, parse `.skill-meta/manifest.json`, verify each listed `files[].sha256` matches on-disk content
**Acceptance criteria:**
- `pack_skill_bundle(src_dir, version) -> Path` produces a `.skillbundle.tar.gz` with correct structure
- `unpack_skill_bundle(bundle_path, dest_dir) -> BundleManifest` returns parsed manifest or raises `BundleStructureError`
- `verify_bundle_contents(manifest, extracted_dir) -> None` raises `BundleContentMismatchError` on any file-hash mismatch
- Tree SHA256 computation is deterministic across pack→unpack→repack cycles
- Unit tests cover: happy-path round-trip, missing `.skill-meta/manifest.json`, file hash mismatch, non-gzip input
**Rationale:** Pure data-layer primitive. Zero deps on network, auth, or skillbox runtime. Runs in isolation, is small in diff, blocks nothing but unblocks the sync pipeline node.
**Validate cmds:**
- `cd .env-manager && python -m pytest tests/distribution/test_bundle.py -v`
- `cd .env-manager && python -c "from runtime_manager.distribution.bundle import pack_skill_bundle, unpack_skill_bundle; print('ok')"`

---

## Node: `signing` — ed25519 manifest signature primitives

**Status:** todo
**Depends on:** (none)
**Writes:** `.env-manager/runtime_manager/distribution/signing.py`, `tests/distribution/test_signing.py`
**Context:** Per-client manifests AND per-bundle manifests are signed with the distributor's ed25519 private key; clients verify with a public key pinned in their config. This node implements *only* the crypto primitives — key loading, sign, verify.
**Contract excerpt:**
- Public key format in config: `"ed25519:<base64-encoded-32-byte-key>"`
- Detached signature format in `.skill-meta/signature.json`: `{"algorithm": "ed25519", "signature": "<base64>"}`
- Manifest signature inside JSON: `"signature": "ed25519:<base64>"` — detached over canonicalized JSON (sorted keys, no whitespace) with `signature` field removed
**Acceptance criteria:**
- `load_public_key(config_str: str) -> Ed25519PublicKey` parses the `ed25519:...` format
- `verify_manifest_signature(manifest_json: dict, public_key) -> None` raises `SignatureVerificationError` on invalid sig; passes with `signature` field removed-then-reinserted
- `verify_detached_signature(payload_bytes, signature_json, public_key) -> None` for bundle signatures
- `sign_manifest(manifest_dict, private_key) -> dict` adds a valid `signature` field (used only in tests + Phase 2 distributor-side tooling; clients only verify)
- Uses stdlib-compatible crypto (`cryptography` library already in skillbox's deps, or adds it to requirements)
- Unit tests cover: valid sig, invalid sig, malformed key, wrong algorithm, field ordering doesn't matter
**Rationale:** Pure crypto layer. Isolatable, well-defined I/O, separately testable. No filesystem or network deps.
**Validate cmds:**
- `cd .env-manager && python -m pytest tests/distribution/test_signing.py -v`

---

## Node: `manifest_schema` — Per-client manifest parse/validate + pin resolution

**Status:** todo
**Depends on:** `signing`
**Writes:** `.env-manager/runtime_manager/distribution/manifest.py`, `.env-manager/runtime_manager/distribution/pin_resolver.py`, `tests/distribution/test_manifest.py`, `tests/distribution/test_pin_resolver.py`
**Context:** The per-client manifest is the curation contract between distributor and client. It's a signed JSON document the client fetches, verifies, and uses to drive sync. Pin resolution is a pure function that answers "given distributor version + min_version + client pin, what version should be installed?"
**Contract excerpt:**
```json
{
  "schema_version": 1,
  "distributor_id": "acme-skills",
  "client_id": "client-42",
  "manifest_version": 14,
  "updated_at": "2026-04-21T10:00:00Z",
  "skills": [
    {"name": "deploy", "version": 8, "min_version": 7, "min_version_reason": "...",
     "sha256": "...", "size_bytes": 28400, "download_url": "/skills/deploy/8/bundle.tar.gz",
     "targets": ["box"], "changelog": "..."}
  ],
  "signature": "ed25519:..."
}
```
Pin resolution rule: `installed = max(min_version or 0, min(recommended, client_pin or recommended))`. Also track which wins: `client`, `distributor`, or `manifest_floor`.
**Acceptance criteria:**
- `parse_manifest(raw_bytes) -> ClientManifest` validates structure, raises `ManifestSchemaError` on missing required fields
- `verify_manifest(manifest, public_key) -> None` delegates to `signing.verify_manifest_signature`
- `resolve_pin(manifest_skill, client_pin: Optional[int]) -> PinResolution` returns `{version: int, pinned_by: "client"|"distributor"|"manifest_floor"|"manifest_recommendation", reason: Optional[str]}`
- Pin test matrix (all must pass):
  - `{client=None, dist=8}` → v8, pinned_by=manifest_recommendation
  - `{client=6, dist=8, min=None}` → v6, pinned_by=client
  - `{client=6, dist=8, min=7, reason="CVE"}` → v7, pinned_by=manifest_floor, reason="CVE"
  - `{client=9, dist=8}` → v8, pinned_by=manifest_recommendation (client pin can't exceed what's offered)
  - `{client=None, dist=8, min=7}` → v8, pinned_by=manifest_recommendation (min doesn't force upgrade if no pin)
- `filter_skills_for_target(manifest, target: Literal["box","laptop"]) -> list[ClientManifestSkill]` drops skills where `targets` doesn't include the target
**Rationale:** The schema + pin resolver is the Phase-1 curation contract. Pure function, exhaustive test matrix, depends only on signing primitives. Blocks sync pipeline but can be built in parallel with bundle format and config schema.
**Validate cmds:**
- `cd .env-manager && python -m pytest tests/distribution/test_manifest.py tests/distribution/test_pin_resolver.py -v`

---

## Node: `config_schema` — Extend skill-repos.yaml with distributor-set + distributors section

**Status:** todo
**Depends on:** (none)
**Writes:** `.env-manager/runtime_manager/shared.py` (additive — new dataclasses + parser branch), `tests/test_config_schema.py`
**Context:** Extend `shared.py`'s config loader to recognize a top-level `distributors:` section and a new `distributor:` source kind alongside existing `repo:` and `path:` entries. This is additive only — existing configs continue to parse unchanged.
**Contract excerpt:**
```yaml
distributors:
  - id: acme-skills
    url: https://skills.acme.dev/api/v1
    client_id: client-42
    auth:
      method: api-key
      key_env: ACME_DISTRIBUTOR_KEY
    verification:
      public_key: "ed25519:..."

skill_repos:
  - distributor: acme-skills      # NEW kind, references distributors[].id
    pick: [deploy, codebase-audit]
    pin:
      deploy: 7
```
**Acceptance criteria:**
- New dataclasses: `DistributorConfig`, `DistributorAuth`, `DistributorVerification`, `DistributorSetSource`
- `load_skill_repos_config(path) -> SkillReposConfig` (or equivalent entry point) parses the new sections without breaking existing `repo:`/`path:` parsing
- `validate_distributor_refs(config)` raises `ConfigError` if a `distributor:` source references an id not in `distributors[].id`
- Existing tests for `repo:` and `path:` source kinds still pass unchanged
- New tests: distributor-only config, mixed config (repo+path+distributor), dangling reference error, missing auth env var (warning not error)
- `auth.key_env` resolution: config parser does NOT resolve the env var at load time — that happens at sync time (test covers this)
**Rationale:** Pure config-layer additive change. No network, no crypto, no sync logic. The sync pipeline node consumes these dataclasses. Highest-churn file (`shared.py` at 3781 lines) — isolate this node so conflicts are detected early in the wave.
**Validate cmds:**
- `cd .env-manager && python -m pytest tests/test_config_schema.py -v`
- `cd .env-manager && python -c "from runtime_manager.shared import load_skill_repos_config; print('ok')"`
- `cd <repo> && make doctor 2>&1 | head -20`  # existing doctor must still pass with existing config

---

## Node: `lockfile_schema` — Enrich lockfile with distributor fields

**Status:** todo
**Depends on:** (none)
**Writes:** `.env-manager/runtime_manager/shared.py` (additive lockfile fields + emitter), `tests/test_lockfile_schema.py`
**Context:** Lockfile gains a `distributor_manifests` top-level section and per-skill distribution fields (`source: "distributor"`, `distributor_id`, `version`, `bundle_sha256`, `pinned_by`, `pin_reason`). Existing fields are unchanged. Existing skills unaffected.
**Contract excerpt:**
```json
{
  "version": 3,
  "config_sha": "...",
  "synced_at": "...",
  "distributor_manifests": {
    "acme-skills": {"manifest_version": 14, "fetched_at": "...", "signature_verified": true}
  },
  "skills": [
    {"name": "deploy", "source": "distributor", "distributor_id": "acme-skills",
     "version": 7, "sha256": "...", "bundle_sha256": "...", "install_tree_sha": "...",
     "pinned_by": "distributor", "pin_reason": "..."}
  ]
}
```
**Acceptance criteria:**
- Additive dataclasses: `DistributorManifestLockEntry`, extended `SkillLockEntry` with optional distribution fields
- `emit_lockfile(entries, distributor_manifests) -> dict` produces the new shape
- `parse_lockfile(raw) -> Lockfile` backward-compatible: lockfiles without `distributor_manifests` or without per-skill `source` parse cleanly and treat source as `repo` (legacy default)
- Existing `validate_skill_repo_sets()` continues to pass against legacy lockfiles
- New tests: emit-parse round-trip, legacy lockfile forward-compat, mixed skills (repo + distributor)
**Rationale:** Also in `shared.py` — but additive and isolated to lockfile section. Coordinate with `config_schema` node via explicit file regions (different sections of `shared.py`); run in same wave only if splits are enforced via `git diff` review.
**Writes coordination:** Because both this node and `config_schema` touch `shared.py`, they MUST be sequenced: config_schema first, then lockfile_schema. Alternatively, factor out to `shared_distribution.py` (preferred — keeps shared.py from growing).
**Validate cmds:**
- `cd .env-manager && python -m pytest tests/test_lockfile_schema.py -v`

---

## Node: `sync_pipeline` — Distributor sync integration in runtime_ops.py

**Status:** todo
**Depends on:** `bundle_format`, `signing`, `manifest_schema`, `config_schema`, `lockfile_schema`
**Writes:** `.env-manager/runtime_manager/distribution/sync.py` (new file — sync logic), `.env-manager/runtime_manager/runtime_ops.py` (additive — dispatcher branch for distributor sources), `tests/distribution/test_sync_pipeline.py`, `tests/distribution/fixtures/` (mock manifest + bundles)
**Context:** The sync loop in `runtime_ops.py` currently dispatches `repo:` and `path:` source kinds through `sync_skill_repo_sets` / similar. This node adds a `distributor-set` branch that: (1) reads credentials from env var, (2) HTTP GETs manifest with `Authorization: Bearer <key>`, (3) verifies manifest signature, (4) stores manifest under `.skillbox-state/manifests/<distributor_id>.json`, (5) iterates skills, (6) applies pick filter + pin resolution + target filter, (7) downloads bundles to `.skillbox-state/bundle-cache/<skill>/<version>.skillbundle.tar.gz`, (8) verifies bundle sha256 + signature, (9) unpacks to temp, (10) calls existing `filtered_copy_skill()`, (11) writes lockfile entry with distribution fields.
**Contract excerpt:**
- Cache layout: `.skillbox-state/bundle-cache/<skill>/<version>.skillbundle.tar.gz`
- Manifest cache: `.skillbox-state/manifests/<distributor_id>.json` (raw JSON as received)
- HTTP headers: `Authorization: Bearer <key>`, `X-Client-ID: <client_id>`, `Accept: application/json` (manifest) or `application/gzip` (bundle)
- Sync is idempotent: re-running with unchanged manifest_version should be a no-op (short-circuits after signature verify)
- Signature verification failure → abort sync of that distributor's skills, leave prior state untouched, surface error; do NOT corrupt lockfile
**Acceptance criteria:**
- `sync_distributor_set(distributor_config, source_entry, cache_root, install_targets) -> list[SkillLockEntry]` returns entries for successful installs
- `runtime_ops.py` dispatcher correctly routes `DistributorSetSource` to `sync_distributor_set` without breaking existing `repo:`/`path:` paths
- Test cases (use `responses`/`httpretty` or custom HTTP mock):
  - Happy path: mock manifest + mock bundle → skill installed, lockfile written
  - Signature invalid → skill NOT installed, prior lockfile unchanged
  - Network error (timeout) → retryable error surfaced, no partial writes
  - Pin resolution matrix integrated: mock manifest with `min_version` → floor respected
  - Target filter: manifest has `targets: ["box"]` and env is laptop → skill skipped
  - Cache hit: if cached bundle sha matches manifest, skip download, still install
  - Unchanged manifest_version: full sync short-circuits to no-op
- Integration test: real `make runtime-sync` against a local fixture server (optional — use pytest-httpserver) succeeds end-to-end
- Backward compat: `make runtime-sync` with existing non-distributor config continues to pass
**Rationale:** This is the load-bearing integration. Cannot parallelize with the primitives. Must come after all dependencies land. Isolated as a new file (`sync.py`) with a single additive line in `runtime_ops.py` to reduce conflict surface.
**Validate cmds:**
- `cd .env-manager && python -m pytest tests/distribution/test_sync_pipeline.py -v`
- `cd <repo> && make doctor 2>&1 | tail -20`
- `cd <repo> && make runtime-sync 2>&1 | tail -40`  # backward-compat smoke test

---

## Node: `doctor_checks` — Distribution integrity checks in validation.py

**Status:** todo
**Depends on:** `sync_pipeline`, `config_schema`, `lockfile_schema`, `manifest_schema`
**Writes:** `.env-manager/runtime_manager/validation.py` (additive — new check functions), `tests/test_doctor_distribution.py`
**Context:** `validate_skill_repo_sets()` already validates tree hashes for repo-sourced skills. Extend with distributor-specific checks that run as part of `make doctor`. All checks are non-blocking (warnings) for offline-friendly operation, except signature mismatch on a cached manifest (that's a hard failure — something tampered with local state).
**Contract excerpt:** five new checks:
1. `distributor_config_valid` — `distributors` section parses and auth env vars are present (warning if missing)
2. `distributor_auth_probe` — `HEAD /manifest` returns 200/304 (warning on failure, success on 200/304/network-error in `--offline` mode)
3. `distributor_manifest_signature` — cached manifest signature verifies against configured `verification.public_key` (HARD FAIL on mismatch)
4. `distributor_bundle_cache_integrity` — cached bundles' stored sha256 matches recomputed file sha (warning — missing cache entries re-download on next sync)
5. `distributor_lockfile_consistency` — lockfile `distributor_manifests` entries reference manifests that exist in `.skillbox-state/manifests/`
**Acceptance criteria:**
- Five new functions named per the checks above, each returning `DoctorCheckResult`
- Registered in the existing doctor check registry (grep for how existing checks are registered)
- Integration test: `python3 .env-manager/manage.py doctor` output includes the new checks with correct pass/fail/warning semantics
- Backward compat: `make doctor` on a repo without `distributors:` config passes with "no distributor configured" notice (not an error)
- Hard-fail test: tamper with a cached manifest signature byte → `make doctor` exits non-zero
**Rationale:** Doctor is the integrity gatekeeper. Runs after sync_pipeline lands so it can validate real sync artifacts. Adds surface to existing validation.py without restructuring it.
**Validate cmds:**
- `cd .env-manager && python -m pytest tests/test_doctor_distribution.py -v`
- `cd <repo> && make doctor`

---

## Node: `auth_surface` — Distributor auth visibility in status/context

**Status:** todo
**Depends on:** `config_schema`, `sync_pipeline`
**Writes:** `.env-manager/runtime_manager/runtime_ops.py` (additive — status reporter), `.env-manager/runtime_manager/context_rendering.py` (additive — CLAUDE.md section), `tests/test_auth_surface.py`
**Context:** Auth is architecture, not transport. Make the distributor connection visible in `make runtime-status`, the MCP server's `skillbox_status` tool, and the generated CLAUDE.md context. "Connected to acme-skills as client-42, 12 skills from distributor, manifest v14, last sync 2h ago."
**Contract excerpt:** status output includes per-distributor summary: `{id, client_id, url, skills_count, manifest_version, last_sync, auth_key_present: bool, auth_probe_result: "ok"|"failed"|"offline"}`.
**Acceptance criteria:**
- `runtime_status()` includes `distributors: [{...}]` when config has a `distributors` section
- `context_rendering.py` adds a "## Connected Distributors" section to generated CLAUDE.md (no-op when no distributors configured)
- Test: rendered CLAUDE.md contains distributor summary when config + lockfile + manifest cache all exist
- Test: no distributor section rendered for legacy config (backward compat)
**Rationale:** Small but visible; completes the "auth as architecture" principle by making it reachable from operator tools. Low-risk additive change.
**Validate cmds:**
- `cd .env-manager && python -m pytest tests/test_auth_surface.py -v`
- `cd <repo> && python3 .env-manager/manage.py runtime-status`

---

## Node: `integration_smoke` — End-to-end fixture server + CI test

**Status:** todo
**Depends on:** `sync_pipeline`, `doctor_checks`, `auth_surface`
**Writes:** `tests/distribution/test_end_to_end.py`, `tests/distribution/fixture_server.py` (local HTTP server for test)
**Context:** Single integration test that runs the full pipeline: spin up a local HTTP server serving a mock manifest + bundle, run `make runtime-sync`, assert skill is installed, run `make doctor`, assert all checks pass.
**Acceptance criteria:**
- Test spins up a `pytest-httpserver`/`http.server` fixture that serves a signed test manifest and a signed test bundle
- Test config references the fixture URL + a test API key
- End-to-end test: config → sync → install → lockfile → doctor → teardown
- Test runs in <30s, deterministic, no real network
- Test leaves no artifacts outside a temp directory
**Rationale:** Final validation before CRAP-scoring. Catches integration bugs that unit tests miss (path resolution, env var reading, real HTTP client behavior).
**Validate cmds:**
- `cd .env-manager && python -m pytest tests/distribution/test_end_to_end.py -v`

---

## Node: `readme_update` — Update README with distributor-set docs

**Status:** todo (final wave)
**Depends on:** `integration_smoke`
**Writes:** `README.md`, `docs/DISTRIBUTION.md` (new — developer guide)
**Context:** README currently documents `skill-repos.yaml` with `repo:` and `path:` sources only. Add `distributor-set` documentation after Phase 1 ships so the docs reflect reality.
**Acceptance criteria:**
- README mentions `distributor-set` source kind with a minimal example
- New `docs/DISTRIBUTION.md` covers: config shape, auth setup, running a distributor (stub — full guide in Phase 2), inspecting cached manifests, troubleshooting signature errors
- No claims about features not yet built (token exchange, device binding, CAS — all Phase 2+)
**Validate cmds:** (docs node — visual review only)

---

## Execution waves (for divide-and-conquer runner)

```
Wave 1 (parallel, no overlap):
  - bundle_format
  - signing
  - config_schema
  - lockfile_schema     ← sequenced after config_schema IF both touch shared.py;
                          otherwise parallel (preferred via shared_distribution.py split)

Wave 2 (parallel, no overlap):
  - manifest_schema     (needs: signing)

Wave 3 (single, all deps land):
  - sync_pipeline       (needs: bundle_format, signing, manifest_schema,
                                 config_schema, lockfile_schema)

Wave 4 (parallel):
  - doctor_checks       (needs: sync_pipeline + schema nodes)
  - auth_surface        (needs: config_schema, sync_pipeline)

Wave 5 (single):
  - integration_smoke   (needs: sync_pipeline, doctor_checks, auth_surface)

Wave 6 (final):
  - readme_update
```

### Write-overlap mitigation

- `shared.py` is 3781 lines; both `config_schema` and `lockfile_schema` touch it. Preferred solution: split `shared_distribution.py` as a new module, keeping `shared.py` edits minimal (one import line each). If the wave runner can't enforce the split, sequence `config_schema` then `lockfile_schema`.
- `runtime_ops.py` is 3595 lines; only `sync_pipeline` and `auth_surface` touch it. Sequence them in different waves (sync_pipeline wave 3, auth_surface wave 4).
- `validation.py` is 1747 lines; only `doctor_checks` touches it. No overlap.

### Hardening gate (post-audit)

After wave 6 lands and `integration_smoke` passes:
1. Run `/crap` against `.env-manager/runtime_manager/distribution/**/*.py` + distribution-touching portions of `runtime_ops.py`, `shared.py`, `validation.py`
2. If `FINAL_SCORE > 30`: run `/mutate` on top 3 hotspots and add tests until score ≤ 30
3. Report surviving hotspots to user with score + file list
