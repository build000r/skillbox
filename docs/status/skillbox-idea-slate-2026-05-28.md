# Skillbox Idea Slate - 2026-05-28

Scope: planning-only output for `skillbox-portfolio-reality-idea-plan-h1k.2`.
No implementation Beads were created here, and no product/code changes were made.
This is dated evidence from 2026-05-28; later push-readiness checks should be
treated as the current source of truth.

## Inputs

- Reality check: `docs/status/skillbox-reality-check-2026-05-28.md`
- Status stack: `diagrams/skillbox-project-status.mmdx`
- Current Beads: `skillbox-smart-20260518-validation-trust-gkn`, `skillbox-portfolio-reality-idea-plan-h1k.3`, `.4`, `.5`, and parent `skillbox-portfolio-reality-idea-plan-h1k`
- Prior adversarial planning: `plans/distribution-phase1/DUELING_WIZARDS_REPORT_R1.md`, `plans/distribution-phase1/DUELING_WIZARDS_REPORT_R2.md`, and `plans/distribution-phase1/plan.md`
- Prior audit signals: `agent_ergonomics_audit/AUDIT_2026-05-17.md` and `agent_ergonomics_audit/audit/recommendations.jsonl`

## Wiki Coverage

No Skillbox-specific registered wiki was found.

- A local private wiki registry was checked and had no Skillbox-specific wiki entry.
- Repo-local search found plans, MMDX files, and prior dueling reports, but no wiki vault or concept-page corpus for Skillbox.

Because there was no wiki grounding to inject, this pass used a plain adversarial idea synthesis grounded in repo docs, Beads, the new reality check, checked-in duel artifacts, and prior audit reports.

## Thirty Candidate Ideas

| # | Candidate | Initial read |
|---:|---|---|
| 1 | Disk-safe broad validation lane | Must-have trust gate; already covered by open validation Bead. |
| 2 | Reconcile current `make doctor`/`make dev-sanity` proof with stale validation-trust notes | Cheap confidence gain; belongs in existing validation Bead. |
| 3 | Active-client expectation repair for `personal` examples | Direct RED from status pass; high onboarding value. |
| 4 | MCP parity cleanup for Claude-only `cm`, `dcg`, and `fwc` | Direct RED from status pass; high agent reliability value. |
| 5 | Pulse daemon regression proof packet | Current pulse is real but dirty/unsettled; high daily-use value. |
| 6 | Standard runtime evidence packet command | Makes future reality checks repeatable and less chat-bound. |
| 7 | Box lifecycle dry-run drill and proof artifact | Important infra safety, but side-effect-adjacent. |
| 8 | RCH/SBH observe-first setup plan | Important because disk pressure is critical; must avoid cleanup mutation. |
| 9 | Context generation drift golden tests | Prevents agent context rot across clients and profiles. |
| 10 | Worker broker fake-Hermes smoke | Proves broker contract without launching real worker runtimes. |
| 11 | `sbp` cwd inference proof refresh | Useful but appears mostly covered by prior skill-mapping work. |
| 12 | README example self-verification snippets | Prevents docs from outliving runtime truth. |
| 13 | MCP resource layer for local skill content | Strong prior duel winner, but not the current operational bottleneck. |
| 14 | Skill ABI design pass | Valuable after MCP resource layer, not first. |
| 15 | Distributor signed-bundle MVP | Strong prior architecture, but broader than current reliability wave. |
| 16 | Client projection acceptance harness | Useful once active-client gap is decided. |
| 17 | Private-config attachment diagnostics | Strong companion to active-client repair. |
| 18 | Dry-run marker TTL and partial-success contract revisit | Prior audit found UX/safety issues; may already be partly remediated. |
| 19 | Fleet health parallelization baseline | Performance candidate for h1k.4, not h1k.2 implementation slate. |
| 20 | SSH candidate cache measurement | Performance candidate for h1k.4. |
| 21 | `.dockerignore` build-context guard | Low-risk hygiene; lower leverage than runtime trust. |
| 22 | Operator credential redaction pass | Security value; needs current source verification before minting. |
| 23 | Session ledger proof examples | Good docs/testing idea, but less urgent than validation trust. |
| 24 | Generated status/pressure summary for agents | Overlaps with evidence packet command. |
| 25 | Default-client/docs policy statement | Lower-cost variant of active-client repair. |
| 26 | Structured MCP parity allowlist | Companion to MCP parity cleanup. |
| 27 | First-box acceptance replay fixture | Valuable but may be expensive under disk pressure. |
| 28 | Runtime profile coverage matrix | Good planning artifact, lower immediate pain. |
| 29 | Distribution-plan stale-claim review | Useful later; not current wave. |
| 30 | Agent ergonomics regression index refresh | Good maintenance; lower than RED/YELLOW gaps. |

## Winnowed Top Five

| Rank | Idea | Why it wins | Implementation shape | Complexity |
|---:|---|---|---|---|
| 1 | Disk-safe validation trust lane | The repo is useful only if agents can trust the local proof surface. Current cheap proof is positive, but broad unittest validation and critical disk pressure remain unresolved. | Update/execute `skillbox-smart-20260518-validation-trust-gkn`: reconcile stale doctor/dev-sanity notes, define a disk-safe broad test path, and close/block with exact evidence. | Medium |
| 2 | Active-client expectation repair | README and Makefile examples lean on `personal`, but this checkout reports no active clients and `--client personal` fails. That is a direct onboarding/runtime truth gap. | Add an implementation Bead to either restore expected private-client attachment diagnostics or update docs/commands to make absence explicit and recoverable. | Small to medium |
| 3 | MCP parity cleanup and policy | `mcp-audit` found valid configs but Claude-only `cm`, `dcg`, and `fwc`. Agents need a deliberate parity rule, not accidental drift. | Add an implementation Bead to decide mirror vs remove vs allowlist, then enforce through `mcp-audit` docs/tests or doctor warning policy. | Small |
| 4 | Pulse daemon regression proof packet | Pulse is running and valuable, but current dirty pulse files make the claim fragile. This is the daily self-healing surface for the box. | Add a focused proof Bead for pulse state/status/log/pressure behavior using existing tests and a no-infra smoke command. | Medium |
| 5 | Standard runtime evidence packet | h1k.1 had to manually stitch doctor/status/pressure/pulse/skills/MCP evidence. A single command/report would reduce future reality-check drift. | Add a planning/implementation Bead for a read-only `stewardship` or `evidence` packet that captures the validated proof lane and artifact paths. | Medium |

## Next Best Ten

| Rank | Idea | Why it remains useful | Current disposition |
|---:|---|---|---|
| 6 | Box lifecycle dry-run drill | Safety-critical infra path, should prove dry-run and guard behavior before real provisioning. | Candidate for h1k.5 after power-map ranking. |
| 7 | RCH/SBH observe-first setup plan | Critical disk pressure makes offload/storage guard valuable, but mutation must stay gated. | Candidate for h1k.5; likely depends on validation trust. |
| 8 | Context generation drift golden tests | Agent context is a core product surface and easy to rot silently. | Candidate implementation Bead. |
| 9 | Worker broker fake-Hermes smoke | Proves worker broker without requiring real Hermes/Codex launch. | Candidate implementation Bead. |
| 10 | README example self-verification snippets | Prevents docs from claiming commands that fail in a default checkout. | Candidate implementation Bead, may merge with active-client repair. |
| 11 | MCP resource layer for local skill content | Prior duel consensus winner with real architecture value. | Defer until reliability wave is green; not current bottleneck. |
| 12 | Skill ABI design pass | Useful bridge between install-time and read-time skill contracts. | Defer until MCP resource layer is accepted. |
| 13 | Distributor signed-bundle MVP | Strong distribution architecture already planned. | Separate distribution epic; do not mix into reliability wave. |
| 14 | Fleet health parallelization baseline | Prior audit found likely performance wins. | Route to h1k.4 profiling/refactor candidate plan. |
| 15 | Operator credential redaction pass | Potential security hardening from prior audit. | Needs current source verification before minting. |

## Plain Duel Scorecard

Two scoring lenses were used because no Skillbox wiki vault was available.

- Reliability wizard: scores immediate operator/agent trust, safety, and proof leverage.
- Platform wizard: scores compounding architecture value, product leverage, and future extensibility.

| Idea | Reliability score | Platform score | Consensus |
|---|---:|---:|---|
| Disk-safe validation trust lane | 970 | 870 | Consensus winner |
| Active-client expectation repair | 900 | 820 | Consensus winner |
| MCP parity cleanup and policy | 880 | 830 | Consensus winner |
| Pulse daemon regression proof packet | 860 | 780 | Consensus winner |
| Standard runtime evidence packet | 800 | 860 | Consensus winner |
| Box lifecycle dry-run drill | 820 | 760 | Strong, but after trust lane |
| RCH/SBH observe-first setup plan | 840 | 720 | Strong, gated by disk/offload policy |
| Context generation drift golden tests | 760 | 790 | Strong, narrower proof surface |
| Worker broker fake-Hermes smoke | 710 | 780 | Useful but not first |
| README example self-verification snippets | 740 | 700 | Useful companion work |
| MCP resource layer | 620 | 900 | Architecturally strong, operationally premature |
| Skill ABI design pass | 560 | 820 | Defer until resource layer has adoption proof |
| Distributor signed-bundle MVP | 520 | 880 | Different epic, not current reliability wave |
| Fleet health parallelization baseline | 640 | 690 | h1k.4 profiling lane |
| Operator credential redaction pass | 760 | 650 | Needs current source verification |

## Dedupe Against Current Beads

| Proposed work | Existing coverage | Dedupe decision |
|---|---|---|
| Disk-safe validation trust lane | `skillbox-smart-20260518-validation-trust-gkn` | Do not mint duplicate; update/execute existing Bead. |
| Broad unittest proof under disk pressure | `skillbox-smart-20260518-validation-trust-gkn` | Do not mint duplicate; it is explicit acceptance there. |
| Active-client expectation repair | No direct implementation Bead | h1k.5 should mint if accepted. |
| MCP parity cleanup and policy | No direct implementation Bead | h1k.5 should mint if accepted. |
| Pulse daemon regression proof packet | Only indirect validation-trust relation; current dirty pulse work exists but no dedicated Bead found | h1k.5 should mint if accepted. |
| Standard runtime evidence packet | No direct implementation Bead | h1k.5 should mint if accepted. |
| Box lifecycle dry-run drill | No direct implementation Bead in current open set | h1k.5 may mint after h1k.3 ranking. |
| RCH/SBH observe-first setup plan | Disk/offload aspects overlap `skillbox-smart-20260518-validation-trust-gkn`; setup plan not directly covered | Consider as dependent follow-up, not duplicate. |
| Fleet health parallelization | h1k.4 profiling/refactor candidate plan should evaluate it | Do not mint here. |
| MCP resource layer and distributor MVP | Prior checked-in plans exist under `plans/distribution-phase1/`; no current reliability-wave Bead | Defer unless h1k.3 ranks platform expansion over reliability. |

## Recommended Accepted Slate For h1k.5

1. Implement/update the existing validation-trust lane, not as a new Bead: `skillbox-smart-20260518-validation-trust-gkn`.
2. Mint a small implementation Bead for active-client expectation repair.
3. Mint a small implementation Bead for MCP parity cleanup and policy.
4. Mint a focused pulse regression proof Bead if the existing dirty pulse work is intended to land.
5. Mint a read-only runtime evidence packet Bead only if h1k.3 confirms the operator value chain favors repeatable proof over new platform features.

Non-goal: do not mint distribution or MCP-resource-layer implementation Beads into this reliability wave unless h1k.3 explicitly changes the priority order.
