# Dueling Wizards Report: Orb Plane Recalibration (2026-07-31)

## Executive Summary

Two models (Claude Fable 5, Codex gpt-5.6-sol) each generated 30 ideas and
winnowed to 5 for making ephemeral orbs first-class portfolio workers
(skills, cass, beads, wiki, dispatch) across sweet-potato, htma, htma_server,
ingredient_server, buildooor. Cross-scoring, reveal, and a blind-spot probe
produced **strong convergence on a six-layer stack**, one killed idea (CC's
capsule scaffolder, 392 — premise factually stale), two repaired ideas, and
six genuinely new blind-spot items neither model initially saw.

Top consensus picks: **content-addressed skill generations** (~854 avg),
**fenced beads command bus** (~862 avg), **session capture spool / memory
escrow** (~816 avg).

## Methodology

- Agents: pane 0 = Claude Code **Fable 5** (spawned `cc=1:fable:xhigh`);
  pane 1 = Codex **gpt-5.6-sol** (observed running at `high` in-pane despite
  `xhigh` spawn pin). NTM session `skillbox-plane`, repo
  `/srv/skillbox/repos/opensource/skillbox`.
- 30 ideas each → top 5 → adversarial cross-scoring 0–1000 → reveal →
  blind-spot probe → this synthesis.
- Both agents did live verification during scoring (not just prose review);
  Codex caught a stale factual premise in CC's list via live reads.

## Consensus Winners (700+ from the opposing scorer, survived reveal)

1. **Content-addressed skill generations** (COD#2 ≈ CC#1 repaired; CC scored
   880, COD scored CC's variant 828). `/v1/skill/resolve` returns a per-repo
   policy-resolved decision set + one `generation_sha256`; bundles served by
   immutable tree hash; orb materializes under
   `generations/<sha>/` and atomically repoints `~/.claude/skills` +
   `~/.codex/skills`. Fixes a verified live defect: `/v1/skill/pull` resolves
   with `cwd=ROOT_DIR` and discards remote `--cwd` (target-blind seam,
   `sbpd.py:368`). Session-pinned generation = reproducible campaign receipts.
2. **Fenced beads command bus** (CC#2 + COD repairs; 845/879). Box remains
   the ONLY `.beads` writer. Orbs get `ready/show/claim/update/worker_done`
   over sbpd; close is **host-authoritative** at reconciliation
   (`close_pending_reconciliation` until the candidate is proven). Double-bound
   authZ (Amp JWT + active DWS lease + fencing token), CAS revision checks,
   idempotency keys, fsync'd mutation journal, `BR_NO_DAEMON=1` allowlisted
   argv, per-repo flock. Git races impossible by construction.
3. **Orb memory escrow** (CC#3 + COD#4 contract; 816/815). Continuous
   content-addressed transcript chunk upload (`POST /v1/worker/cass/chunks` +
   `/seal`), flush per completed turn (never teardown-only — E2B kill may
   skip teardown). Verbatim encrypted raw archive (preserves Amp
   transcript-digest proofs) + separate redacted Cass projection +
   Amp `threads export` correlation. Sessions are `sealed | truncated |
   indeterminate` — never silently lossy. Wiki rides this lane: read routes +
   provenance-bound proposal inbox; host-side ingest gates unchanged.
4. **Registry + doctor + staged activation** (COD#5 ≈ CC#4 repaired; CC
   scored 820). `workspace/orb-workers.yaml` desired-state registry;
   `sbp orb adopt` (validate/patch existing capsules — **never** scaffold
   over them: all five targets already have sealed `.agents/setup`, `resume`,
   `amp-context/capsule.json`); `sbp orb doctor --all` as the single release
   matrix. Activation: shadow tick → amputation merge (after its gate) →
   epoch-open + re-arm in one operation → ring 1 sweet-potato → ring 2 HTMA
   fixed-point (must include cycle-chef) → ring 3 buildooor (requires an
   explicit `rob_driven` policy decision, not plumbing).

## Contested → Resolved

- **WorkerSessionManifest** (COD#1, CC scored 730): both agree identity is
  needed; converged on a *slimmed* version — claims-derived repo-set binding
  in ONE middleware, minimal session state, `SBP_TOKEN_CMD` auto-refresh of
  the 10-minute Amp JWT (401 currently strands long orb sessions). Not a
  second identity spine; a `worker_plane` projection of the existing DWS
  attempt.
- **Arm the tick** (CC#5, COD scored 593): value confirmed, sequencing
  rejected — "everything downstream is already built" was too strong; the
  new lanes (skills/beads/cass) don't exist yet, so arming is the LAST
  promotion of the staged rollout. Per-repo round-robin killed (conflicts
  with component-scoring); rings are admission ceilings, not schedulers.

## Killed

- **Capsule scaffolder as ranked** (CC#4, 392; CC fully conceded): the
  CAPSULE_ABSENT premise was stale — live reads show all five repos have
  complete capsules. Scaffolding would overwrite reviewed, repo-specific
  bootstrap encoding the htma/htma-server/ingredient-server/cycle-chef
  fixed-point. Survivors: registry + doctor, folded into consensus #4.

## Blind Spots (neither model saw pre-probe; not cross-scored — extra scrutiny)

- **B1 Venue partitioning of the work graph** (CC): beads carry no notion of
  where they're executable; a venue-blind orb-only tick will select
  components whose top work an orb cannot do (ingredient_server release lane,
  browser-bound oracle beads, prod-secret work). Fix: `venue:*` /
  `needs:*` labels + component venue feasibility + doctor lint. Also the
  honest resolution of the amputation debate: routing as metadata, one work
  graph, two lanes.
- **B2 Merge blast-radius classes** (CC): `merge_coupling: inert |
  auto-release | prod-adjacent | control-plane` per repo, as publish-gate
  modifiers. ingredient_server merges sit one habitual operator command from
  deploy; sweet-potato breakage manufactures its own future queue via the
  issue-report loop; buildooor is the operator's own instrument panel
  (fleet editing its supervision surface = observability loss at the worst
  moment).
- **B3 Lease visibility** (CC): the RepoSetLeaseRegistry is invisible to NTM
  swarms, local sessions, and the operator — the held-lease window is
  unprotected in both directions. Fix: sbp banner + NTM spawn guard +
  `LOCAL_SESSION_ACTIVE` admission exclusion; read-only integrations.
- **B4 Orb data firewall** (COD): everything protects the box from orb
  writes; nothing protects operator data from orb egress. Portfolio-wide
  `gh` token must never enter an orb — scoped short-lived GitHub App tokens
  per repo-set, three egress profiles (offline/build/research), redacted
  task packets (no raw SPAPS PII), fail admission when enforcement is
  unavailable.
- **B5 Work Passport** (COD): executor-neutral phase placement + fenced
  handoff across orb → box-NTM → Mac/operator lanes (v1: exactly three
  profiles, linear handoffs; start with ingredient_server build→release and
  sweet-potato code→feedback splits). Also the designed continuation path
  after orb death.
- **B6 Verified-value ledger** (COD): spend gates exist, economics loop
  doesn't. Append-only outcome ledger over existing receipts (typed unknown
  costs, never invented dollars) + a daily operator morning brief
  ("needs you: ingredient_server exact image ready; Mac release auth
  required"). Report-only first; bounded feedback into DWS later.

## Score Matrix

| Idea | Origin | Self-Rank | Opp. Score | Verdict |
|---|---|---|---|---|
| Skill plane manifest+lockfile | CC | 1 | 828 | MERGED into generations |
| Leased beads write door | CC | 2 | 879 | WIN (with host-close repair) |
| Session capture spool | CC | 3 | 816 | WIN (with verbatim-archive contract) |
| Capsule scaffolder+registry+doctor | CC | 4 | 392 | KILLED as ranked; registry+doctor survive |
| Arm orb-only tick | CC | 5 | 593 | REPAIRED → staged activation last |
| WorkerSessionManifest | COD | 1 | 730 | SLIMMED → one middleware |
| Skill generations | COD | 2 | 880 | WIN (consensus top) |
| Beads command bus | COD | 3 | 845 | WIN |
| Memory escrow (cass+wiki) | COD | 4 | 815 | WIN |
| Registry + staged DWS activation | COD | 5 | 820 | WIN |

## Meta-Analysis

- CC trusted a contract document (do-work-son SKILL.md's CAPSULE_ABSENT
  claim) where Codex ran live reads; Codex's mean for CC's list (701.6) vs
  CC's mean for Codex's (818) is, on the evidence, the right ordering — CC
  conceded this explicitly.
- CC bias: composition-first, doctrine-preserving, ranked by risk posture.
  Codex bias: identity/contract-first, occasionally over-engineered
  (manifest ceremony conceded post-reveal).
- Adversarial pressure measurably improved the design: host-authoritative
  close, generation model, `sbp orb adopt` semantics, and ring staging all
  emerged from cross-review, not from either original list.

## Recommended Next Steps

Build order (converged): 1) identity middleware + token refresh → 2) skill
generations → 3) cass capture + wiki reads/proposals → 4) beads reads then
fenced writes → 5) adopt/registry/doctor across the five repos → 6) shadow
tick → rings → operator-gated arm. Blind spots B1/B3/B4 land before ring 1;
B2 before ring 2; B6 with first real dispatches; B5 v1 with ingredient_server
ring.

Artifacts: WIZARD_IDEAS_{CC,COD}.md, WIZARD_SCORES_{CC_ON_COD,COD_ON_CC}.md,
WIZARD_REACTIONS_{CC,COD}.md, WIZARD_BLINDSPOTS_{CC,COD}.md (all 2026-07-31).
