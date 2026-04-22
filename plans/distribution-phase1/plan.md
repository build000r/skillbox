# Distribution Phase 1 — Skillbox as Multi-Client Skill Distribution Platform

## Source of Truth

Accepted architecture: `plans/distribution-phase1/DUELING_WIZARDS_REPORT_R2.md`.

This plan is the executable distillation of R2's Phase 1 action plan. R2 is treated as Phases 0–5 of the domain-planner template (Landscape, Core Value Gate, Discovery, Contract, Strategy). This file covers Phase 6 handoff. No interactive refinement was added because both R1 and R2 already converged under adversarial pressure.

## Core Value Gate

- **Primary actor:** *skillbox distributor* — an operator who curates and ships skills to multiple clients.
- **User-visible outcome:** A distributor can publish a signed skill update once, and any client (box or laptop) who runs `make runtime-sync` receives only the skills curated for them at the pinned versions, fully verified and usable offline thereafter.
- **Minimum winning slice:**
  1. `distributor-set` source kind in `skill-repos.yaml` + `distributors` top-level config section
  2. Signed per-client manifest (JSON + ed25519)
  3. Signed bundle format (`.skillbundle.tar.gz`)
  4. Bundle cache directory as v1 artifact store
  5. Sync pipeline extension that fetches → verifies → extracts → runs `filtered_copy_skill()` → updates lockfile
  6. Enriched lockfile (new section + per-skill distribution fields)
  7. Doctor integration (signature + cache + auth checks)
  8. API-key auth as first-class config
  9. Two-layer pin with distributor `min_version` floor
- **Explicit non-goals (Phase 2/3):**
  - Short-lived token exchange, device binding, scoped tokens
  - Background auto-sync (`launchd`/cron)
  - CAS (content-addressed store) — bundle cache directory is v1 substitute
  - Staged rollout rings
  - jsm adapter (`provider: jsm`)
  - Standalone laptop CLI (`skillbox list/rollback/diff`)
  - Laptop-mode installation semantics (client reads same `skill-repos.yaml` — laptop CLI wrapper is Phase 2)
- **Debt avoided:** token lifecycle management, index DB consistency, multi-distributor arbitration, fleet-scale GC policies. These become essential only at >5 clients and are cheap to add post-Phase-1.

## Invariants (both rounds)

1. **Lockfile is canonical local truth.** Every distribution-sourced skill produces lockfile entries with tree hash, bundle hash, manifest version. `validate_skill_repo_sets()` continues to be the integrity gate.
2. **No live remote content fetch.** Content mutates only through an explicit sync event that the lockfile records. Metadata freshness checks (opt-in, Phase 2) are the only acceptable network contact at agent/session time.
3. **Auth gates sync, never execution.** Revoked key means "no more updates," not "installed skills stop working."
4. **Additive only.** Existing `repo:` and `path:` source kinds continue to work unchanged. `filtered_copy_skill()` is reused, not replaced.
5. **Explicit systems over hidden magic** (VISION Value #3). Manifest is signed and inspectable as a file, not a database row.

## Contract — Config shape (additive to skill-repos.yaml)

```yaml
version: 3  # bump when ready; v2 remains supported

distributors:
  - id: acme-skills
    url: https://skills.acme.dev/api/v1
    client_id: client-42
    auth:
      method: api-key
      key_env: ACME_DISTRIBUTOR_KEY
    verification:
      public_key: "ed25519:abc123..."   # distributor's signing key, pinned in config

skill_repos:
  - repo: build000r/skills            # existing kind — unchanged
    ref: main
    pick: [audit-plans]

  - path: ./workspace-skills          # existing kind — unchanged
    pick: [scratch]

  - distributor: acme-skills          # NEW kind
    pick: [deploy, codebase-audit]
    pin:
      deploy: 7
```

## Contract — Per-client manifest (JSON)

```json
{
  "schema_version": 1,
  "distributor_id": "acme-skills",
  "client_id": "client-42",
  "manifest_version": 14,
  "updated_at": "2026-04-21T10:00:00Z",
  "skills": [
    {
      "name": "deploy",
      "version": 8,
      "min_version": 7,
      "min_version_reason": "CVE-2026-xxxx: prompt injection in rollback handler",
      "sha256": "def456...",
      "size_bytes": 28400,
      "download_url": "/skills/deploy/8/bundle.tar.gz",
      "targets": ["box"],
      "changelog": "Added rollback safety checks"
    }
  ],
  "signature": "ed25519:..."
}
```

## Contract — Bundle format

```
<skill>-v<version>.skillbundle.tar.gz
├── SKILL.md
├── references/...
└── .skill-meta/
    ├── manifest.json       # {name, version, tree_sha256, min_skillbox_version, tags, files:[{path,sha256}]}
    ├── signature.json      # detached ed25519 over manifest.json
    ├── changelog.md
    └── compatibility.json  # {min_skillbox_version, agent_tags}
```

## Contract — Lockfile enrichment

```json
{
  "version": 3,
  "config_sha": "...",
  "synced_at": "...",
  "distributor_manifests": {
    "acme-skills": {
      "manifest_version": 14,
      "fetched_at": "...",
      "signature_verified": true
    }
  },
  "skills": [
    {
      "name": "deploy",
      "source": "distributor",
      "distributor_id": "acme-skills",
      "version": 7,
      "sha256": "def456...",
      "bundle_sha256": "...",
      "install_tree_sha": "xyz...",
      "pinned_by": "distributor",
      "pin_reason": "v8 has a known issue with Docker deploys"
    }
  ]
}
```

## Contract — Pin resolution rule

```
installed_version = max(
    manifest.min_version or 0,
    min(
        manifest.version,                       # distributor's recommended
        client_pin or manifest.version
    )
)
```

- Distributor's hard floor (`min_version`) overrides everything below it.
- Within the allowed range, client's pin takes precedence.
- `pinned_by` in the lockfile records the winner: `client`, `distributor`, or `manifest_floor`.

## Contract — Doctor checks (added to validation.py)

- `distributor_config_valid` — `distributors` section parses; auth env var is set
- `distributor_auth_probe` — `HEAD /manifest` with auth returns 200/304 (warning, not blocking; offline-friendly)
- `distributor_manifest_signature` — stored manifest signature verifies against configured public key
- `distributor_bundle_cache_integrity` — cached bundles' file SHAs match their manifest.json
- `distributor_lockfile_consistency` — lockfile `distributor_manifests` entries reference real cached manifests

## Rejected Approaches (carried from R2 duel)

| Approach | Why rejected |
|---|---|
| Runtime MCP fetch of skill content (ui.sh-style) | Scored 934 against in R1; breaks operator control of update timing |
| SQLite as canonical state store | jsm's SQLite corrupted on this machine during investigation; lockfile is more robust, inspectable, diff-able |
| Lease-backed content overlays | Breaks `validate_skill_repo_sets()` invariant — installed SHA drifts from lock SHA without a sync event |
| jsm federation as core | Commercial coupling risk; jsm API returned 500s + SQLite corruption during investigation; impedance mismatch (user-catalog vs distributor-fleet) |
| Two-lane stable/live architecture | Subsumed by manifest-level `version` + `min_version` + client pin |
| Signed bundles killed as replacement (R1 verdict) | Reversed: R1 rejected for single operator; R2 distribution use case needs a packaging unit for clients without git access |

## Tech choice justifications

| Decision | Chosen | Rationale |
|---|---|---|
| Signature algorithm | ed25519 | Small, fast, deterministic, stdlib support via `cryptography` |
| Bundle format | `.tar.gz` | Universal (unlike `.tar.zst` which isn't guaranteed); gzip available on every client |
| Auth transport | API key in env var → HTTP `Authorization: Bearer <key>` | Simple, server-to-server, no browser OAuth dance for Phase 1 |
| Manifest format | JSON (not MessagePack/CBOR) | Inspectable with `jq`/`cat`, diff-able in git, signature is detached |
| State store | Enriched lockfile (JSON) | Existing validation.py reads it; SQLite explicitly rejected |
| Cache layout | `.skillbox-state/bundle-cache/<skill>/<version>.skillbundle.tar.gz` | Human-readable; v2 CAS migration is a mechanical rename to hash-indexed layout |

## Out of scope (explicit)

Items not to implement in Phase 1 even if tempting:

1. **Short-lived runtime token exchange** — API key directly for Phase 1. Token exchange adds session state, token refresh semantics, and another failure mode.
2. **Device binding / per-device revocation** — `client_id` flat in Phase 1. Device scoping adds entity modeling.
3. **CAS (content-addressed store)** — Bundle cache is named-by-(skill,version). v2 renames to hash-addressed.
4. **Background update check** — Manual `make runtime-sync` only in Phase 1.
5. **Staged rollout rings** — Distributor publishes single manifest per client in Phase 1.
6. **jsm adapter** — `provider: jsm` entry point deferred.
7. **Standalone laptop CLI** (`skillbox list/diff/rollback`) — In Phase 1, the laptop runs the same `manage.py` path or a thin wrapper.
8. **Test/demo distributor server** — Phase 1 implements the CLIENT side. A reference distributor server (skillbox-dist CLI + Cloudflare Worker API) is a Phase 2 artifact.

## Success criteria (binary)

1. `make runtime-sync` succeeds against a mock `distributor-set` with a test manifest + signed bundle.
2. Bundle signature verification failure aborts install without modifying install targets.
3. Lockfile reflects distributor origin + version + bundle hash for distributor-sourced skills.
4. `make doctor` reports all 5 new distributor checks with correct pass/fail/warning semantics.
5. Mixed config (`repo:` + `path:` + `distributor-set`) all sync correctly in a single `make runtime-sync` invocation.
6. Pin resolution test matrix passes: `{client=None, dist=v8}` → v8; `{client=v6, dist=v8, min=7}` → v7; `{client=v6, dist=v8, min=None}` → v6; `{client=v9, dist=v8}` → v8 (client pin can't exceed what's offered).
7. `python3 .env-manager/manage.py doctor` and existing validation paths continue to pass for non-distributor skill sets (backward compatibility).
