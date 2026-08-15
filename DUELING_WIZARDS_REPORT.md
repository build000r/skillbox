# Dueling Idea Wizards Report: skillbox — the `sbp test` epic

Untracked duel material (holds local paths; do not commit unscrubbed).

## Executive Summary

Two models each generated 30 ideas and winnowed to 5 on the `sbp test` mechanism
and its sequencing. The duel produced **near-total convergence on five pillars**
(both models independently arrived at the same five), one genuinely contested
policy (green-cache default), two withdrawn ideas, and a blind-spot round in
which **both models independently identified the same missing #1 category**
(workload containment). Top consensus picks: (1) receipts as a three-axis
never-lie state machine, (2) a sealed test-plan as the authority boundary with
local-first sequencing, (3) hybrid dirty-tree source identity built on
`git write-tree` plus a capsule manifest. The synthesized epic is encoded as a
beads graph rooted at the `sbp-test` epic.

## Methodology

- Agents used (observed at runtime via NTM status): Claude Code 2.1.232 running
  model `fable` at xhigh (pane `sbp-test-duel__cc_1_fable`) and codex-cli
  0.147.0 running `gpt-5.6-sol` at xhigh (pane `sbp-test-duel__cod_1_gpt-5.6-sol`).
- Shared brief: `DUEL_CONTEXT_SBP_TEST.md` (box.py lifecycle trace, sequencing
  proposal, two-prong framing, sweet-potato exemplar).
- Ideas: 30 per agent, winnowed to 5 (both exposed their full 30: CC as an
  appendix, COD as a winnowing table — Phase 4b expansion unnecessary).
- Scoring: adversarial cross-model, 0–1000, with code-level fact verification.
- Phases: study → ideate → cross-score → reveal → blind-spot probe → synthesize.
- Overlap check: the two top-5 sets map 1:1 onto the same five pillars —
  strong independent convergence, the highest-value validation signal this
  method produces.

## The Five Consensus Pillars (merged designs)

1. **Local-first sequencing with a sealed plan** (CC#1 835 ⊕ COD#1 870).
   Ship manifest → compiler → local wave executor → receipts before any remote
   work; every slice independently useful. The compiler's product is an
   immutable `test-plan/v1` binding source identity; the remote worker never
   re-reads the repo. Corrections adopted: reuse `topological_layers`/
   `blast_radius` but the concurrent executor is real new work; `critical_path`
   is node-count-only today (weighting is new code; timeouts are ceilings, not
   estimates); global slot cap + per-unit resource groups are v1 requirements;
   freeze manifest+capsule+minimal-plan first, let attempt/receipt schemas
   harden through the local slice.
2. **Hybrid dirty-tree source capsule** (CC#2 760 ⊕ COD#2 830). Identity =
   `git read-tree HEAD` → `add -A` (temp index) → `write-tree` OID; plus
   `capsule_manifest_sha256` (materialized bytes + policy) and
   `archive_sha256` (transport). Conceded on both sides: `.gitignore` is NOT a
   secret firewall (fail-closed secret-path screening via
   `scripts/lib/redaction.py`); unreferenced tree objects are GC-prunable
   (retained archive is the durable evidence); refuse dirty submodules in v1.
3. **Detached, two-phase remote dispatch — finish the broker** (CC#4 705 ⊕
   COD#3 790). Both halves required: durable admission-before-launch +
   idempotent attempt fencing (COD) AND detached process-group-owned execution
   + separate retryable harvest (CC). Killed: CC's runner-shipped-in-archive
   (wrong trust boundary — withdrawn by originator); COD's oversized
   `executor-capabilities/v1` (cut to a minimal preflight). Implementation:
   harden the existing worker broker (`start_new_session` at
   `_shared/worker.py:1049`, PID+boot-id identity, box taint as explicit
   state), not a second runner.
4. **Three-axis receipt state machine** (CC#3 receipts half ⊕ COD#4 885 — the
   duel's highest single score). test outcome × execution outcome × proof
   completeness, reducer-derived with a tested validity matrix, append-only
   attempts, authoritative-indeterminate finalization, never-lie verdicts
   mapped to the existing exit ladder. Record cache-key material from day one.
5. **Readiness scorer + refactoring skill as one contract** (CC#5 745 ⊕ COD#5
   855). Stable finding codes are the API: every code has a skill recipe,
   enforced by contract tests. Evidence states (proven/likely/unknown/blocked/
   n-a); only named, evidenced blockers gate; the numeric rollup is advisory
   ("the scorer is the gate" — withdrawn by originator). Serial-oracle ladder
   with coverage-equivalence proof; probes bounded, opt-in, in capsule
   workspaces.

## Contested (left for operator judgment, sequenced so it doesn't block)

**Green-cache default policy** — the duel's only surviving disagreement.
CC (post-correction): default-on skipping for iteration runs matches the
estate's own exemplar (sweet-potato's `make pytest` short-circuits on receipt
match today); with env-digest key + `cache: never` + provenance + fresh-only
proof mode it is the biggest everyday win. COD: execute-by-default in v1;
opt-in reuse only after input closure is proven; "incorrectly skipping a test
is a much worse product failure than rerunning a green unit." **Agreed by
both:** receipts land first; the key must include an executor environment
digest; proof-bearing runs are always fresh. The epic sequences the cache last
and records the default-policy decision as an explicit operator call informed
by pilot evidence.

## Killed Ideas

- Runner shipped inside the tested archive (CC appendix #27) — conceded: the
  workload must not own the code that writes its own verdict.
- Emitting sweet-potato's `full-gate.json` from partial receipts without a
  coverage-equivalence contract (CC appendix #23) — gated.
- Freezing five schemas together before the first slice (COD) — self-withdrawn.
- "The scorer is the admission gate / convergence is a number" (CC) —
  withdrawn; Goodhart pressure on agents is real.
- The brief's own sequencing (remote dispatch as the hard gate; golden image as
  step 2) — both models independently inverted it and fact-checked the brief:
  the `skillbox-worker` golden image is zero-planned (not half-planned), the
  git-clone deploy path is dead code, snapshot/restore fabric capabilities are
  not landed.

## Blind Spots (Phase 6.9 — neither model's original five)

- **Workload containment** (BOTH, independently — instant consensus): test
  code is untrusted third-party code running root-equivalent
  (`usermod -aG sudo,docker`) on boxes holding the DO token and a tailnet
  identity. Containment ladder: systemd-run cgroup fences (also kills the
  OOM-takes-down-tailscaled failure mode and turns box-taint into a checkable
  cgroup-empty receipt) → isolated containers; synthetic homes; env allowlists;
  a result channel the child cannot write; fix `_worker_loaded_result`'s
  missing-result→`succeeded` default for test attempts.
- **Skillbox is the zeroth consumer** (CC): `self-test.sh` is already a
  proto-`sbp test` (lanes, isolated checkout, pinned matrix, receipts with
  schema id, flock, a daily blocking consumer via pre-push). Adopting it first
  prevents a receipt schism and generates the stage-timing telemetry the
  golden-image decision needs.
- **Environment materialization** (CC): nothing between "capsule extracted"
  and "command runs" — `setup:` units (existing inputs/outputs/success_check
  vocabulary) + a lockfile-keyed content-addressed env store; the env
  fingerprint doubles as the receipt's executor environment digest.
- **Verification-obligation ledger + repro packets** (COD): `sbp test --for
  <work-ref>` satisfies a pre-declared requirement (satisfied/failed/
  indeterminate/stale); `test-repro/v1` gives the next agent a bounded
  reproduction after the box is gone; graph edges bead→requirement→receipt→
  capsule surface into `brain.next`.
- **Shadow-mode adoption authority** (COD): per-repo state machine wrapped→
  shadowing→candidate→authoritative→rolled_back with a predeclared-threshold
  pilot report (concordance hard stop, completeness, indeterminate rate,
  economics); sweet-potato pilot runs both gates from the same capsule.

## Score Matrix

| Idea | Origin | Self-rank | Opponent score | Post-reveal | Verdict |
|---|---|---:|---:|---|---|
| Local-first inversion | CC | 1 | 835 | corrections adopted | CONSENSUS — first lane |
| Sealed test-plan/v1 + sequence | COD | 1 | 870 | schema-freeze scope cut | CONSENSUS |
| Git tree identity | CC | 2 | 760 | merged into hybrid capsule | CONSENSUS (merged ~850) |
| Source capsule | COD | 2 | 830 | adopts write-tree core | CONSENSUS (merged ~850) |
| Receipts + green-cache | CC | 3 | 505 | receipts kept; cache split out | CONTESTED (cache only) |
| Two-phase dispatch | COD | 3 | 790 | adds detachment; attestation cut | CONSENSUS (merged ~850) |
| Detached execution | CC | 4 | 705 | runner-in-archive withdrawn | CONSENSUS (as "finish broker") |
| Receipt state machine | COD | 4 | 885 | vocabulary self-repaired | CONSENSUS — top score |
| Finding-code scorer | CC | 5 | 745 | "scorer is gate" withdrawn | CONSENSUS (merged ~850) |
| Evidence ladder + skill | COD | 5 | 855 | adopts finding codes | CONSENSUS (merged) |

Cross-scores mean: CC→COD 846; COD→CC 710 (dragged by the 505). Neither side
found a fabrication in the other; every error on both sides was a precision
error, several conceded within minutes when confronted with file:line evidence.

## Meta-Analysis

- **Claude's biases:** overclaims reuse and leverage ("the scheduler already
  exists", "~50 lines", ".gitignore means secrets can't ride"), leans on
  estate precedent to justify aggressive defaults (cache-on). Its strength is
  code-anchored synthesis — every claim carried a file:line, and its fact-check
  of the brief set the duel's factual floor.
- **Codex's biases:** contract-first ceremony (five frozen schemas), under-reuse
  of existing machinery (never cited the graph algorithms; hand-rolled tree
  identity), and conservatism that lumped a deterministic cache in with ML
  duration prediction. Its strength is failure-mode enumeration and trust-
  boundary instincts (runner ownership, workload isolation, adoption authority).
- **Where adversarial pressure paid:** the reveal produced genuine design
  movement, not posturing — CC withdrew its runner-in-archive and its cache key;
  COD adopted write-tree, detachment, and finding codes and self-downgraded its
  own receipt vocabulary (`command_failed` axis conflict) unprompted. The
  blind-spot round's instant convergence on workload containment is the
  strongest possible signal that it belongs in the epic.

## Recommended Next Steps

Encoded as the beads graph under the root `sbp-test` epic (see
`plan:sbp-test` label): P1 local proof kernel (front door, manifest, capsule,
sealed plan, local executor, receipts, zeroth consumer) → P2 remote leg
(finish the broker via the existing `skillbox-fabric-remote-dispatch-os9j`
bead, minimal preflight, capsule transport, workload containment, artifacts) →
P3 readiness lane ∥ P4 economics/provisioning safety → P5 agent loop +
adoption (ledger, repro, shadow adoption, sweet-potato pilot, then the cache).

Duel artifacts: WIZARD_IDEAS_{CC,COD}.md, WIZARD_SCORES_{CC_ON_COD,COD_ON_CC}.md,
WIZARD_REACTIONS_{CC,COD}.md, WIZARD_BLINDSPOTS_{CC,COD}.md.
