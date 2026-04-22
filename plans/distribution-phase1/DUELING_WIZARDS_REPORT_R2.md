# Dueling Idea Wizards Report — Round 2: skillbox as a multi-client skill distribution platform

## Executive Summary

**Round 1** asked "should skillbox adopt ui.sh's remote MCP architecture for a single operator?" and concluded: *reject the remote transport; add a local MCP resource layer.* (See `DUELING_WIZARDS_REPORT_R1.md`.)

**Round 2** reframed the question: the operator becomes a **skill distributor** shipping curated skill updates to paying/trusted clients who run those skills both inside their own skillbox instance AND on their local laptop. API-key auth, pin/rollback, per-client curation, fleet consistency, offline operation after sync. A third architecture (**jsm**, Jeffrey's Skills Manager — subscription package manager at `jeffreys-skills.md`, 107 skills installed on this machine) was studied live for the first time.

Both agents (Claude Code and Codex/gpt-5.3-codex xhigh) were explicitly required to steelman positions Round 1 rejected. After independent ideation, harsh adversarial cross-scoring, and a reveal phase with concrete pressure questions, both converged on **the same merged architecture** despite starting from opposite framings (CC pipeline-first, Codex component-first).

**The Round 2 verdict reverses three Round 1 positions under the distribution use case:**
1. ✅ **Signed bundles** — promoted from *killed as replacement* to *primary distributor→client transport*
2. ✅ **Central auth as architecture** — promoted from *not discussed* to *first-class identity + entitlement + revocation plane*
3. ⚠️ **Remote network contact at session** — partially reversed: *no live content fetch* (still rejected), but *lease-backed metadata-only freshness* is acceptable opt-in

**Consensus final architecture:** `feed source in skill-repos.yaml → signed per-client manifest → signed versioned bundles → artifact store abstraction (bundle cache v1 / CAS v2) → filtered_copy_skill() → enriched lockfile as single source of truth`.

## Methodology

- **Agents:** cc (Claude Code, Opus 4.7 in pane 0) + cod (gpt-5.3-codex xhigh in pane 1) via NTM swarm in `<repo>`
- **Round 2 phases:** study (3 architectures + R1 synthesis + live jsm inspection) → ideate (10 → 5 with explicit steelman mandate) → cross-score (0–1000, with probe questions) → reveal with pressure prompts → react → synthesize
- **Ground truth studied:** skillbox codebase (runtime_ops.py, validation.py, shared.py, mcp_server.py, client overlays), skillbox docs (README.md, VISION.md, DUELING_WIZARDS_REPORT_R1.md), ui.sh architecture via `mcp__uidotsh__uidotsh_fetch`, jsm CLI live (`jsm --help`, `jsm whoami`, `jsm list`, config.toml, SQLite state, 107 installed skills). During investigation: jsm's SQLite was corrupted (`database disk image is malformed`); jsm API returned `INTERNAL_ERROR: Failed to fetch skill versions`. These empirical reliability signals informed the architecture.
- **Artifacts:** `WIZARD_IDEAS_{CC,COD}_R2.md`, `WIZARD_SCORES_{CC_ON_COD,COD_ON_CC}_R2.md`, `WIZARD_REACTIONS_{CC,COD}_R2.md`

## Round 1 → Round 2 Verdict Reversal Audit

| Round 1 verdict | R1 Score | Round 2 status | Trigger for change |
|---|---:|---|---|
| Reject live remote fetch of skill content | 934 | **Still held** — but refined to "no live *content* fetch; lease-backed *metadata* freshness is acceptable" | The content-vs-metadata distinction was implicit in R1; both agents now agree metadata is categorically different |
| Signed bundles killed as replacement for git repos | 260 | **Reversed** — now primary distributor→client transport | R1's ceremony-cost argument applied to the single operator; in R2 the distributor bears ceremony, client gains simplicity |
| Central auth not discussed (single-op had no use for it) | — | **New load-bearing architecture** | Auth IS the commercial product relationship; enables per-client curation, device-scoped revocation, scoped tokens |
| Two-lane stable/live killed | 220 | **Still killed** — subsumed by per-client manifests + channel semantics in the manifest itself | No system-level lane needed; manifest-level channel field suffices |
| Keep git/file/lock trust anchor | — | **Preserved absolutely** | Lockfile as canonical local truth is Codex's non-negotiable invariant #1 after the reveal |
| Local MCP resource layer (R1's top priority) | 872 | **Orthogonal — still valid** | Distribution architecture sits above; resource layer mediates consumption regardless of origin |

**The rule that emerges:** *live remote content delivery* is rejected in both rounds. *Authenticated sync-time delivery of signed packaged content* is rejected in R1 (no need) and accepted in R2 (core requirement). The dividing line is not "network = bad" but "content mutates without an explicit sync event that the lockfile records."

## Round 2 Score Matrix

| # | Idea | Origin | Self-rank | Cross-score | Post-reveal | Verdict |
|---|---|---|---:|---:|---|---|
| 1 | Per-client signed manifest (curation contract) | CC | 3 | **918** | **850** | **Architectural center of gravity. Highest score entire duel.** |
| 2 | Distributor feed as skill-repos.yaml source type | CC | 1 | 892 | 820 | **Consensus anchor.** Cleanest bridge from skillbox-today. |
| 3 | Signed bundles as primary transport (STEELMAN) | CC | 4 | 861 | **800** | **R1 reversal confirmed.** CC now incorporates Codex's CAS pattern. |
| 4 | Central auth as first-class trust infrastructure (STEELMAN) | CC | 2 | 846 | **790** | **Consensus.** CC adopts Codex's short-lived token exchange + device scoping. |
| 5 | Client-scoped signed bundle registry | COD | 1 | **790** | — | Merged with CC #3. **CAS + target-env scoping are Codex's genuine architectural contributions.** |
| 6 | Auth-first control plane | COD | 3 | 700 | — | Merged with CC #4. **Device revocation + scoped tokens** were Codex's best details CC missed. |
| 7 | Near-live update awareness via HEAD / MCP (STEELMAN) | CC | 5 | 694 | **600** | Survives as **opt-in, metadata-only, lease-backed, default off**. |
| 8 | Near-live MCP mediation with lease-backed content overlay | COD | 2 | **540** | — | **Conceded by Codex.** Content overlays "cross the integrity boundary." Reduced to metadata-only freshness. |
| 9 | Twin-target installer + SQLite ledger | COD | 4 | **520** | — | **Inverted by Codex.** SQLite demoted to optional disposable index; lockfile stays canonical. |
| 10 | jsm federation mode | COD | 5 | **340** | — | **Demoted by Codex.** "Useful reference, optional adapter, not core architecture." |

## Consensus Winners (both agents align after reveal)

### 1. Per-client signed manifest as the curation contract
**Scored 918 — the highest of any idea across both rounds of the duel.**

The manifest is the data contract between distributor and client — *what* should be installed, at what versions, with what pin policy, targeting which environment. It's signed, inspectable, diffable, cacheable, and recorded in the lockfile's `distributor_manifests` section. Every other R2 idea (feed, auth, bundles, updates) serves this contract.

**Why it's the center of gravity:** as CC self-reflected, "you can't ship distribution without knowing what to distribute to whom." Everything else is plumbing.

```json
{
  "schema_version": 1,
  "distributor_id": "acme-skills",
  "client_id": "client-42",
  "manifest_version": 14,
  "skills": [
    {
      "name": "deploy",
      "version": 8,
      "min_version": 7,
      "min_version_reason": "CVE-2026-xxxx",
      "sha256": "...",
      "download_url": "/skills/deploy/8/bundle.tar.gz",
      "targets": ["box"],
      "changelog": "..."
    }
  ],
  "signature": "ed25519:..."
}
```

### 2. Distributor feed as a skill-repos.yaml source type
Additive: new `kind: distributor-set` (or `feed:`) alongside existing `repo:` and `path:`. API-key auth via env var, client_id identifies the client. Materializes to local files via the existing `filtered_copy_skill()` pipeline. No parallel universe — feeds pass through the same validation, lockfile, and doctor plumbing as git-sourced skills.

### 3. Signed versioned bundles as primary distributor→client transport (R1 reversal)
`.skillbundle.tar.gz` (or `.skillpack.tar.zst` — format bikeshed) containing SKILL.md + references/ + `.skill-meta/` with manifest.json, changelog, compatibility, ed25519 signature. Client verifies signature against distributor's pinned public key before extraction. Historical versions cached locally for instant rollback without re-download. Uniquely enables air-gapped distribution (USB/email transfer) — impossible with git-clone or API-only delivery.

**Why R1 was wrong for R2:** R1's ceremony argument assumed the operator was authoring and the operator was consuming. R2 splits these — the distributor bears the packaging ceremony (a feature of their publishing workflow), the client gets a verified self-contained package without needing git access to source repos or SSH keys per skill.

### 4. Central auth as first-class trust infrastructure (new)
Auth is architecture, not an HTTP header detail. Config surfaces it (`distributors` section separate from `skill_repos`), doctor validates it (lightweight auth probe), status reports it ("connected to acme-skills as client-42, 12 skills, last sync 2h ago"), lockfile records it per skill (distributor_id + client_id + manifest_version).

**Codex's refinements CC adopted in reveal:**
- Long-lived API key → short-lived (15–60 min) scoped bearer token for session
- Device binding: `distributor → client → device` with per-device revocation (stolen laptop scenario)
- Scoped operations: `manifest:read`, `bundle:read`, `telemetry:write` as distinct permissions
- **Auth gates sync, never execution.** A revoked key means "no more updates" not "your installed skills stop working."

### 5. Artifact store abstraction from v1, CAS implementation phased
Define an artifact-store interface in the architecture immediately. v1 implementation: simple bundle cache directory (`.skillbox-state/bundle-cache/<name>/<version>.skillbundle.tar.gz`). v2 implementation: content-addressed storage with dedupe, atomic projection, GC policies. Higher-level contracts don't change between v1 and v2.

**Why CAS matters (CC conceded under pressure):**
- Instant rollback without re-download (content already local)
- Atomic installs (projection failure doesn't corrupt install target)
- Deduplication across `.claude/skills/deploy` and `.codex/skills/deploy`

### 6. Lockfile as canonical local truth (Codex conceded; CC held)
The existing lockfile is extended with distribution metadata (`distributor_id`, `manifest_version`, `version`, `bundle_sha256`, `pin_reason`). SQLite is explicitly rejected as state store after Codex's concession — jsm's corrupted `jsm.db` on this very machine was the decisive empirical evidence. SQLite is acceptable ONLY as a disposable, rebuildable acceleration index derived from the lockfile.

## Contested Resolved in Reveal

### Lease-backed content overlay (Codex #2) — *partially conceded to metadata-only*
CC's argument: overlays break `validate_skill_repo_sets()`'s `installed_sha == lock_sha` invariant, creating non-reproducibility between clients whose leases refresh at different times (Client A @ 10am vs Client B @ 2pm see different content despite identical lockfiles).

Codex's concession after reveal: **"Yes, for instruction-content overlays, CC's argument holds."** Replaced with **lease-backed metadata-only freshness** — leases apply to manifest version notices, changelog snippets, advisories; never to skill instruction text.

Session-pinned overlay variant (content fetched with its own hash and recorded in lockfile at session start, no mid-session refresh) was considered but is effectively "a mini-sync, not near-live drift." Dropped.

### Emergency hotfix propagation — *resolved via the blind-spot idea below*

## Killed Ideas (mutual after reveal)

- **Pure jsm federation as core architecture.** Commercial coupling risk + impedance mismatch (jsm is user-catalog, not distributor-fleet) + undocumented proprietary API + observed reliability issues (500s + SQLite corruption during this investigation). Survives only as: optional adapter for clients who already use jsm + learn from jsm's UX patterns (integer versioning, --offline flag, list/versions/diff/changelog command surface).
- **SQLite as primary state store.** Observed fragility on this exact machine. Two-sources-of-truth problem if lockfile becomes a generated projection. Inspectability loss (`sqlite3 binary` vs `cat`). Acceptable only as disposable rebuildable index.
- **Content overlays via lease refresh.** Breaks lockfile integrity by design.
- **Four-level entity hierarchy (distributor → org → seat → device → environment).** Over-engineered for described use case. Two levels suffice for v1 (distributor → client with optional device binding). Org and seat abstractions can be added when actually needed.
- **Session-start HEAD check as default.** Even when scoped to metadata, default should be manual/background — session-start check is opt-in only. Lease-based caching reduces network calls from "every init" to "every N hours" regardless.

## The R2 Blind-Spot Idea (emerged only through adversarial exchange)

### `min_version` hard floor + background auto-sync

Neither agent proposed this in ideation. It emerged in CC's reveal as the honest bridge between Codex's push for fast emergency updates and CC's refusal to allow content overlays.

**The problem it solves:** the distributor discovers a critical bug in deploy v6 (prompt injection, credential leak, whatever). How does the fix reach all clients quickly without mid-session content mutation?

**The mechanism:**
1. Manifest gains a `min_version` field separate from `version` (recommended):
   ```json
   {"name": "deploy", "version": 8, "min_version": 7, "min_version_reason": "CVE-2026-xxxx"}
   ```
2. Pin resolution rule becomes: `max(min_version, min(recommended_version, client_pin))` — distributor's hard floor overrides the client's pin; within the allowed range, client pin controls.
3. Optional `update_check: background` mode runs a launchd/cron job every N hours. If it detects `min_version` violations, it **auto-syncs** the affected skills (downloads bundle, verifies, installs, updates lockfile).
4. Next session uses the fixed version from local files. **Lockfile is accurate throughout.**

**Why this wins the content-overlay debate without crossing the line:**
- Fast emergency updates: achievable within the background sync interval (tunable)
- Lockfile integrity: preserved absolutely (updates happen via full sync events)
- Client control: preserved for non-security updates (regular `min` rule applies)
- Transparency: `min_version_reason` tells the client *why* their pin is being overridden

This is the Round 2 equivalent of Round 1's "Skill ABI" blind-spot — a genuinely new idea that neither framing produced alone but that both camps accept after the exchange.

## Meta-Analysis

### Model bias signatures (consistent across both rounds)

**Claude Code (CC):**
- Pipeline-first thinking ("my five ideas are really one distribution pipeline + one optional enhancement")
- Strong on argumentative structure ("spent more time on WHY Round 1's verdicts change")
- Self-critical under pressure (caught his own "five ideas = one pipeline" self-critique both rounds)
- Harsh scorer (min 340 R2 vs R1's min 220)
- Empirical grounding (cited jsm's specific corruption + 500s as architecture evidence)

**Codex (gpt-5.3-codex xhigh):**
- Component-first thinking (modular primitives: CAS, ledger, mediation, federation, channels)
- Strong on schema-level implementation detail (config types, entity hierarchies, token lifecycles)
- Generous scorer (min 694 R2)
- Over-ambitious on new primitives (leases for content, SQLite as primary — both walked back in reveal)
- Highest concession rate: demoted his own #1, #2, #4, #5 after CC's pressure

### Round-over-round learning

Both agents learned from Round 1 and applied that learning in Round 2:
- **CC explicitly audited R1 verdicts at the top of his R2 ideation** (table showing what reversed/held/changed)
- **Codex opened R2 with "the objective function changed"** and immediately flagged which R1 rejections needed revisiting
- **CC's self-critique ("five ideas = one pipeline") recurred** — a consistent structural insight about his own tendency to over-fragment

### What convergence means this round

R1 and R2 both ended with both agents on the same revised execution order — different *reasoning paths* converging on *the same architecture*. This is a strong triangulation signal: when two models with different biases arrive at the same answer under adversarial pressure, the answer is probably close to correct.

**But the R2 convergence is stronger than R1's.** R1 converged on "what to build for me." R2 converged on "what to build for a distribution product" — with explicit steelman mandates forcing each agent to defend positions they didn't naturally hold. That both still arrived at the same merged architecture after steelmanning opposing positions is harder evidence.

### The single most important R2 insight

**Centralize delivery and control; localize execution.** (Codex's one-line crystallization.)

Everything in the consensus architecture serves this rule:
- Central: manifest, auth, bundles, catalog, curation, pin policy
- Local: installed files, lockfile, MCP server, skill execution, doctor, runtime
- Network contact: sync-time only (for bundles + manifests), metadata-only freshness checks (opt-in)
- Offline after sync: always

This rule reconciles Round 1's "reject remote" with Round 2's "enable distribution" — the remote layer is about *control plane* (what should be installed per client), the local layer is about *execution* (how installed skills run). Neither round disagrees with this; R1 just didn't have a control plane to worry about.

## Concrete Action Plan

### Phase 1 — Ship the distribution pipeline (MVP for one distributor, one client type)

1. **Extend `skill-repos.yaml` schema** with `distributor-set` source kind + `distributors` section (separate from skill_repos)
2. **Manifest format + signature scheme** — define JSON schema, ed25519 signing, `distributor_id` + `client_id` + `manifest_version` fields
3. **Signed bundle format** — `.skillbundle.tar.gz` with `.skill-meta/{manifest.json,signature.json,changelog.md,compatibility.json}`
4. **Bundle cache directory** (v1 implementation of the artifact-store abstraction): `.skillbox-state/bundle-cache/<skill>/<version>.skillbundle.tar.gz`
5. **Sync pipeline extension** — teach `manage.py sync` to: fetch manifest, verify signature, filter by pick + pin + targets, download bundles, verify bundle signatures + tree hashes, extract to temp, run `filtered_copy_skill()`, update lockfile
6. **Enriched lockfile schema** — add `distributor_manifests` section, per-skill `source: distributor`, `distributor_id`, `version`, `bundle_sha256`, `pin_reason`, `pinned_by`
7. **Doctor integration** — validate signatures present, bundle cache integrity, manifest freshness, auth config valid
8. **Central auth** — `distributors` section with `auth.method: api-key` + `auth.key_env`; doctor auth probe; status shows connected distributors

### Phase 2 — Harden for small fleet (5–20 clients, one distributor)

9. **Short-lived token exchange** — long-lived API key → 15–60 min scoped bearer for sync session
10. **Per-device auth binding + revocation** — `device_id` in auth exchange; distributor can revoke a specific laptop without affecting the box
11. **Two-layer pin with `min_version` floor** — `max(min_version, min(recommended, client_pin))` resolution rule + `min_version_reason` transparency
12. **Target environment scoping** — `targets: ["box"]` / `["laptop"]` / `["box", "laptop"]` per skill in manifest; sync skips non-applicable skills per environment
13. **Background update check** (opt-in) — launchd/cron job with lease-cached manifest freshness; auto-syncs on `min_version` violations
14. **Client laptop CLI** — `skillbox list`, `skillbox versions <skill>`, `skillbox rollback <skill> <version>`, `skillbox sync`, `skillbox doctor` reading from lockfile (no SQLite)

### Phase 3 — Scale (larger fleet, multiple distributors, v2 primitives)

15. **CAS implementation** — replace bundle-cache-by-name-and-version with content-addressed store; projection to multiple install targets; GC policy
16. **Staged rollout rings** — distributor publishes different manifest versions to different client rings; rings are distributor-side operational pattern, no client-side change needed
17. **Optional jsm adapter** — `provider: jsm` entry that shells out to `jsm sync --org <dist>` and imports state into the lockfile; bridge, not coupling
18. **Audit trails + telemetry** — `telemetry:write` scope; distributor gets sync timeline per client; privacy-respecting (client controls opt-in)
19. **Orthogonally: Round 1's local MCP resource layer** — independent from distribution, mediates consumption regardless of origin; both rounds' priorities ship in parallel

### What explicitly NOT to build (validated by both rounds)

- **Live remote content fetch at runtime.** Rejected in both rounds. Content mutates only via sync events that update the lockfile.
- **SQLite as canonical state store.** Empirically fragile on this exact machine.
- **jsm federation as core.** Commercial coupling + impedance mismatch + reliability signals.
- **Four-level entity hierarchy.** Two levels suffice; add only when actually needed.
- **System-level stable/live lanes.** Manifest-level channels subsume this.
- **Cross-session content overlays.** Lockfile invariant must hold.

---

## Bottom line

**Round 1 answered:** for a single operator, a local MCP resource layer is the right architecture; reject ui.sh's remote transport.

**Round 2 answered:** when the operator becomes a distributor serving clients across cloud+local environments, add a distribution pipeline **above** the local resource layer — feeds, per-client signed manifests, signed bundles, central auth, bundle-cache-to-CAS evolution — while preserving the lockfile as canonical local truth and never crossing the content-overlay line.

**The rule that reconciles both rounds:** *centralize delivery and control; localize execution.* Sync-time network contact is about **what should be installed per client**. Runtime remains purely local, deterministic, lockfile-governed.

**What changed between rounds is not the architectural principles — it's the problem's scope.** R1's principles (operator controls timing, lockfile is truth, explicit systems) still hold in R2. R2 just adds a control plane above them for the distributor→client relationship.

---

**Appendix — Round 2 artifact files:**
- `WIZARD_IDEAS_CC_R2.md` (34k, 10→5 with steelman mandate)
- `WIZARD_IDEAS_COD_R2.md` (11k, 10→5)
- `WIZARD_SCORES_CC_ON_COD_R2.md` (23k, harsh but concedes CAS + device-auth + lease semantics + target scoping)
- `WIZARD_SCORES_COD_ON_CC_R2.md` (8k, generous, 918 high score for manifest)
- `WIZARD_REACTIONS_CC_R2.md` (19k, 6 concrete concessions + 3 held lines + blind-spot synthesis)
- `WIZARD_REACTIONS_COD_R2.md` (9k, 4 major reversals: content overlay → metadata-only; SQLite primary → disposable index; CAS day-1 → phased; jsm core → optional adapter)
