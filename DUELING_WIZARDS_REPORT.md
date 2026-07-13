# Dueling Idea Wizards Report: Skillbox (Round 3)

## Executive Summary

GPT-5.6 SOL Max and Claude Fable 5 High independently generated 30 ideas each,
winnowed them to five, scored the opposing portfolio, confronted the scores,
rebutted, steelmanned, and then probed for shared blind spots. Four ideas
survived the strict 700+ post-reveal consensus bar:

1. **Single-mutator discipline** — one state-root writer lock across public
   mutation boundaries and pulse, with truthful holder metadata. Consensus
   mean: **885**.
2. **One executable command contract** — begin with a coverage/parity ratchet
   that catches CLI/Make/MCP/safety drift, then generate only mechanical
   boundary material. Consensus mean: **840**.
3. **Evidence-aware box lifecycle reducer** — typed provider outcomes and
   mandatory evidence for high-risk transitions. The reducer wins; the full
   digital-twin simulator is deferred. Consensus mean: **763**.
4. **Federated `doctor --all` health protocol** — one read-only, prioritized
   front door over existing health providers. Generic automatic repair does
   not survive; typed, allowlisted remediation must be a separate earned
   feature. Consensus mean: **740**.

The duel also discovered two current defects that should be fixed before any
large architectural project: DigitalOcean read failures can be mistaken for
confirmed droplet absence, and the Make/MCP teardown wrappers omit the
confirmation required by `box.py down`.

## Methodology

- **Models:** Codex CLI 0.144.1 with `gpt-5.6-sol` on the requested Max route
  (rendered by the installed CLI as `xhigh`); Claude Code 2.1.170 with
  `claude-fable-5 --effort high`.
- **Isolation:** labeled NTM session `skillbox--duel-sol-fable-r3`; prior
  wizard artifacts were excluded from independent study and ideation.
- **Idea volume:** 30 candidates per model, privately winnowed to five.
- **Scoring:** 0–1000, with utility and pragmatism weighted 2× and
  accretiveness 1.5×.
- **Phases:** project study → blind ideation → repo-grounded cross-score →
  score calibration → reveal → rebuttal → steelman → blind-spot probe →
  orchestrator synthesis.
- **Calibration:** Codex initially scored all five Claude ideas above 700.
  The required criticality pass widened its range from 746–922 to 445–884.
- **Beads:** open and closed work were checked for overlap. No beads were
  created because `--beads` was not requested.

Score means below use the calibrated opponent score and the originator's
post-reveal self-score. Steelman scores are shown as additional evidence, not
silently folded into the arithmetic.

## Consensus Winners

### 1. Single-mutator discipline — 885 consensus

- **Origin:** Claude, self-rank #4.
- **Scores:** Codex 884; Claude post-reveal 885.
- **Why it survived:** Skillbox has multiple concurrent mutation actors—human,
  coding-agent swarms, and pulse—but current locks protect individual state
  files rather than multi-step invariants. A conservative state-root writer
  lock is small relative to the corruption class it prevents.
- **Required scope:** lock a small, documented set of public mutation
  boundaries rather than only CLI dispatch; carry explicit ownership through
  nested calls; leave reads and true dry-runs unlocked; make coverage
  testable so new mutators cannot bypass it.
- **Important concession:** no `locks --clear` unlock verb. Kernel `flock` is
  authoritative; stale metadata may be cleaned only after acquiring the lock.
- **Residual risk:** incomplete boundary inventory, descriptor inheritance,
  long health waits while locked, multiple state roots, and non-POSIX filesystems.

### 2. Executable command contract — 840 consensus

- **Origin:** Codex, self-rank #1.
- **Scores:** Claude 865; Codex post-reveal 815; Claude steelman 885.
- **Why it survived:** a live three-surface failure proves the thesis. The
  real `box.py down` requires confirmation, but `make box-down` and the
  operator MCP teardown path omit it. Registry metadata also covers 41 rich
  command specs while the live CLI reports 63 commands.
- **Strongest implementation:** start with a lint-only ratchet and a checked-in
  allowlist of existing gaps. New drift fails CI; the allowlist may only
  shrink. Then generate or lint mechanical MCP schemas, docs, completions,
  aliases, and wrapper forwarding. Merge the two command registries last, if
  still valuable.
- **Hidden value exposed by steelman:** declared `risk` and `side_effect`
  metadata are currently descriptive rather than dispatch-enforced. Making
  policy load-bearing is more important than generating documentation.
- **Residual risk:** argparse features do not map cleanly into one schema;
  Make argv construction is difficult to inspect; wrong enforced risk
  metadata can be worse than missing metadata.

### 3. Evidence-aware lifecycle reducer — 763 consensus

- **Origin:** Codex, self-rank #3.
- **Scores:** Claude 750; Codex post-reveal 775.
- **Why it survived:** `do_get_droplet()` currently returns `None` for every
  nonzero `doctl` result. `confirm_droplet_absent()` then treats `None` as
  observed absence, contradicting its own promise that read errors never
  confirm destruction. Network, authentication, and rate-limit failures are
  therefore conflated with a genuine not-found result.
- **Winning scope:** introduce typed provider results—found,
  confirmed-not-found, retryable failure, permanent failure, malformed
  response—and route only high-risk transitions through an evidence-aware
  reducer first: destroy, firewall lockdown, Tailscale enrollment, and
  recovery.
- **What did not survive:** five generalized provider protocols, broad
  property-generated schedules, and a public `box.py simulate` surface before
  the reducer demonstrates missing test coverage.
- **Residual risk:** even the smaller change touches high-consequence lifecycle
  code and must preserve resume/idempotency semantics.

### 4. Federated `doctor --all` — 740 consensus

- **Origin:** Claude, self-rank #1.
- **Scores:** Codex 734; Claude post-reveal 745; Codex steelman approximately
  810 for the narrowed protocol.
- **Why it survived:** excellent validators exist, but users and fresh agents
  must already understand the architecture to pick the right one. Existing
  `evidence.py` aggregation is useful substrate, yet its doctor section covers
  runtime doctor rather than the outer reconcile and structure layers.
- **Winning scope:** a provider protocol with stable check IDs, scope,
  severity, freshness, explicit unavailable/timed-out states, provenance, and
  one prioritized next action. Providers remain authoritative and existing
  commands keep their contracts.
- **What did not survive:** executing display-oriented `fix_command` shell
  strings. Any future repair path must use a tiny allowlist of typed action
  IDs, structured arguments, fresh preconditions, mutation locking, and
  postcondition checks—and must be justified separately by repeated failures.
- **Residual risk:** a doctor-of-doctors can become slow, noisy, or a dependency
  magnet without bounded concurrency and conservative prioritization.

## Score Matrix

| Idea | Origin | Self-rank | Opponent score | Post-reveal self | Mean | Verdict |
|---|---|---:|---:|---:|---:|---|
| Single-mutator discipline | Claude | 4 | 884 | 885 | **885** | Consensus win |
| Executable command contract | Codex | 1 | 865 | 815 | **840** | Consensus win; steelman 885 |
| Lifecycle reducer + simulator | Codex | 3 | 750 | 775 | **763** | Reducer wins; simulator deferred |
| One Doctor | Claude | 1 | 734 | 745 | **740** | Read-only federation wins; generic fix split out |
| Brain read-path latency bundle | Claude | 2 | 645 | 670 | **658** | Strong narrowed kernel; raw persistent cache rejected |
| Plan → apply → verify receipts | Codex | 2 | 640 | 650 | **645** | Narrow high-risk pilot; steelman ~700 |
| Pulse as fast read plane | Codex | 5 | 660 | 610 | **635** | Merge into measured latency program |
| Durability autopilot | Claude | 3 | 571 | 595 | **583** | Correct goal, over-broad implementation |
| State Guardian | Codex | 4 | 570 | 430 | **500** | Original bundle retired as duplicative |
| Flight-recorder timeline | Claude | 5 | 445 | 510 | **478** | Defer; narrow three-source CLI only after operation IDs |

## Contested and Narrowed Ideas

### Read-path performance: keep the measured kernel

Claude's latency program and Codex's pulse read plane independently converged
on the same problem, which validates the problem but not either architecture.
The surviving combined program is:

1. measure cold startup, model construction, graph work, and each adapter;
2. add a stable, non-flaky regression proof;
3. parallelize independent adapters with bounded concurrency and truthful
   timeout/provenance states;
4. cache only if residual profiling justifies it, and cache a sanitized,
   versioned brain projection—not the secret-adjacent resolved runtime model.

Codex's steelman scored this narrowed discipline around 785. It is a strong
follow-on, but the original cache bundles did not earn consensus.

### Receipts: telemetry-first, external-drift-only

The universal receipt protocol failed the value-to-complexity test. The
steelman rescued a smaller sequence:

- unify the two marker stores without behavior change;
- emit redacted run-ID receipts as telemetry before using them for gating;
- pilot precondition-bound plans only for high-risk operations affected by
  external drift, such as teardown and upgrade;
- allow honest `interrupted-unknown` outcomes instead of promising crash-exact
  transaction logs;
- stop after telemetry if enforcement never demonstrates additional value.

### Durability: compose shipped primitives instead of rebuilding them

Backup, verify, drill, guarded restore, and stewardship drill-age evidence
already ship under closed epic `skillbox-backup-restore-epic-mtpq`. The useful
remaining work is operator-side scheduled create/verify, recovery-coverage
validation, remote retrieval proof, and only then retention. Custom SigV4,
in-box possession of operator credentials, and new duplicate backup verbs were
rejected.

### Timeline: a reader may be useful, causality is not yet available

Claude established that the runtime journal has producers but no reader. That
supports a bounded CLI view after high-trust operation IDs exist. It does not
support a five-source causal narrative, Tier-1 registration, MCP exposure,
search integration, or indexes before incident demand and correlation quality
are demonstrated.

## Retired Components and Ideas

There was no strict mutual kill below 300. Adversarial pressure nevertheless
retired several bundles or components:

- **State Guardian as written:** the originator conceded it re-pitched shipped
  create/verify/drill/restore functionality. A narrower recovery-coverage
  contract survives.
- **Raw persistent resolved-model cache:** rejected because it can duplicate
  secret-derived state and requires an unproven invalidation contract.
- **Generic shell-backed doctor `--fix`:** rejected in favor of typed,
  allowlisted actions if repeated evidence later justifies them.
- **Built-in S3 SigV4 replication:** rejected as security-sensitive plumbing
  outside Skillbox's core; use an established operator-side tool.
- **Full causal flight recorder:** deferred until shared operation identity and
  real incident demand exist.

## Blind-Spot Findings

The blind-spot phase produced two independent convergence clusters and several
new categories. These did not receive adversarial cross-scores, so the scores
below are provisional orchestrator estimates rather than consensus scores.

| Blind spot | Independent signal | Provisional score | Assessment |
|---|---|---:|---|
| Brain Quality Lab / orientation gauntlet | Both models independently proposed it | **810** | Highest-value new idea: evaluate whether `next` and generated context improve safe agent outcomes, not only latency/schema correctness |
| Skill update review + input provenance | Both models independently identified agent-input trust | **780** | Start with diffing changed skill commits/tree hashes and re-hashing installed trees; broader provenance labels need careful claims |
| Operator escalation channel | Claude-only | **760** | Small multiplier for every health proof: notify only on transitions with cooldowns; avoid alert fatigue |
| Swarm resource governor | Codex-only | **740** | Advisory admission for builds/tests/archive work could prevent retry-driven self-denial; attribute honestly and avoid early cgroup complexity |
| Manifest consolidation ratchet | Claude-only | **715** | Lint derivable cross-manifest facts before generating any public manifest sections |
| Client forgetting contract | Codex-only | **690** | Contrarian and important for client boundaries; begin with inventory/export plans because deletion proof is hard |
| Unified state GC | Claude-only | **675** | Real need, but deletion blast radius requires an allowlist, dry-run, and never-prune-last-verified invariants |
| Ecosystem compatibility contract | Codex-only | **670** | Probe only the three most failure-sensitive external tools; avoid an unmaintainable version matrix |

The strongest blind-spot insight is methodological: making the brain faster and
more complete can amplify bad advice. A replayable quality corpus should exist
before performance and command-coverage improvements dramatically increase
brain usage.

## Meta-Analysis

### Model tendencies observed

- **Claude/Fable 5 High** favored operator-visible health, concurrency safety,
  and operational evidence. Its strongest work was grounded and concrete, but
  it twice overstated absence proofs: `br search` hid closed beads, and a grep
  for `flock`/`fcntl` missed locking through an imported helper.
- **GPT-5.6 SOL Max** favored contracts, state machines, and generalized
  protocols. Its initial cross-scores were too generous; calibration improved
  the duel substantially. Its strongest later contribution was aggressive
  scope-cutting and separating a useful kernel from an architectural bundle.
- **Shared bias:** both models initially under-credited shipped capabilities
  and treated machine integrity as the whole product. Closed-bead inspection
  and the blind-spot probe corrected that.

### Where adversarial pressure improved the result

- The originator of State Guardian reduced it from a product proposal to a
  narrow recovery-coverage idea after learning how much already ships.
- Claude removed an unsafe `locks --clear` concept from its own winning idea.
- Both models rejected shell-string repair execution and converged on typed
  remediation.
- Steelmanning changed real positions: Claude withdrew two central attacks on
  phased receipts; Codex reframed latency as safety/compliance and One Doctor
  as a federated health protocol.
- The duel produced a better combined sequence than either original portfolio:
  contract parity + mutation safety + typed lifecycle truth, then health UX and
  measured performance.

## Recommended Next Steps

1. **Fix the verified droplet-absence bug now.** Introduce typed provider
   outcomes and regression tests proving auth/network/rate-limit failures
   cannot confirm destruction.
2. **Fix teardown surface drift now.** Forward confirmation through Make and
   operator MCP correctly, then use this defect as the first golden case for a
   command parity lint.
3. **Implement the state-root mutation lock.** Inventory public mutators and
   pulse entry points; prove mutual exclusion, crash release, nested ownership,
   timeout behavior, and read availability.
4. **Ship the contract ratchet.** Lint live CLI, registry, MCP, Make, aliases,
   and safety forwarding; fail only on new drift while shrinking the existing
   allowlist.
5. **Add a Brain Quality Lab before broad brain acceleration.** Start with
   15–20 redacted scenarios, multiple acceptable actions, explicit forbidden
   actions, evidence-grounding checks, and abstention quality.
6. **Build the read-only federated doctor.** Extend existing evidence providers
   with outer reconcile and structure health; measure adoption before adding
   any remediation action.
7. **Profile the brain, then optimize.** Parallel adapters and a stable CI proof
   are the first likely wins; caching remains conditional.

## Evidence Artifacts

- `WIZARD_IDEAS_CC_R3.md`, `WIZARD_IDEAS_COD_R3.md`
- `WIZARD_SCORES_CC_ON_COD_R3.md`, `WIZARD_SCORES_COD_ON_CC_R3.md`
- `WIZARD_REACTIONS_CC_R3.md`, `WIZARD_REACTIONS_COD_R3.md`
- `WIZARD_REBUTTAL_CC_R3.md`, `WIZARD_REBUTTAL_COD_R3.md`
- `WIZARD_STEELMAN_CC_R3.md`, `WIZARD_STEELMAN_COD_R3.md`
- `WIZARD_BLINDSPOTS_CC_R3.md`, `WIZARD_BLINDSPOTS_COD_R3.md`
