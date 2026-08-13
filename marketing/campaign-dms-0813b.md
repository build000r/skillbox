# Skillbox family marketing campaign dms-0813b

Status: implementation draft; public adoption blocked on usage rights
Admitted family baseline: 2026-08-13
Wiki prior: sealed packet `sha256:bf1723477d0dc2979c13524253b3db5a6e8c7dc7b3b42c16cddabd219849a9ab`, zero bytes and zero entries

## Authority and boundaries

- Outbound authority: none. Ads, posts, submissions, email, outreach, and spend stay drafts or human approval items.
- Production authority: none. No deploy or publication.
- Primary public product surface selected for delivery: `build000r/skillbox` README and docs.
- Private `skillbox-config`, `skills-private`, and `personal-plugins` are evidence/source surfaces, not public destinations.
- No secrets, private transcripts, customer PII, fabricated proof, or unverified metrics may enter campaign copy.

## Specialist routing

The 37-skill overlay was treated as provenance, not implicit activation. This pass explicitly invoked:

`do-marketing-son`, `marketing`, `repo-landing-cro`, `power-map`, `wiki`, `wiki-duel`, `product-marketing`, `customer-research`, `copywriting`, `cro`, `content-strategy`, `seo-audit`, `ai-seo`, `analytics`, `ads`, `ad-creative`, `social`, and `community-marketing`.

`wiki-duel` stopped at its documented no-coverage boundary because the sealed wiki packet is empty. Repository evidence and required Oracle rounds provide the adversarial input instead; no unsealed wiki was read or written.

## Evidence ledger

| Claim | Evidence | Tier | Allowed copy |
|---|---|---|---|
| Skillbox is designed for one operator-owned private machine and coding agents | `docs/VISION.md`; `docs/status/skillbox-power-map-2026-05-28.md` | verified repository contract | “A durable, private Linux workstation for one operator and their coding agents.” |
| Durable homes, repos, logs, and client overlays are part of the runtime model | `README.md`; `workspace/runtime.yaml`; `docs/runtime-graph.md` | verified internal | Name these components; do not claim universal persistence under every failure. |
| A captured first-box run resolved 4 repos, 11 services, 7 logs, 18 checks and ended 15 pass/1 warning/0 fail | `examples/first-box-demo.md`, captured at `e6f21b0` | dated internal proof | Cite date and commit; label operator proof, not customer result or current-main proof. |
| Managed boxes target Tailnet-only host access after enrollment | `docs/VISION.md`; `docs/tailnet-only-lifecycle.md` | verified contract | Describe intended topology and proof commands; do not say certified or universally secure. |
| There are permissioned customer testimonials | `docs/field-reports/reports.jsonl` has no report rows | verified absence | Say no permissioned customer reports exist; never invent a quote. |
| Skillbox has a paid plan, hosted service, or support SLA | no pricing/checkout surface found; README license boundary | unknown/absent | State that none is defined. |
| A public reader may install or execute Skillbox | README says reading/reference only and forbids reuse without permission; no top-level license grant | verified blocker | Invite inspection only. Do not use installation as a public conversion until rights are granted. |
| Acquisition, install, activation, and retention rates | analytics probe returned `none`; research lists gaps | unknown | Do not claim rates or causal lift. |
| Family members provide adjacent public workflows | admitted README/VISION files in `skills`, `clawgs`, `swimmers`, and `notes-grep` | verified internal | Use as architecture context; do not imply bundled installation or universal integration. |

## Money and power map

- **End user:** the coding agent consumes runtime truth; the human operator directly uses CLI/SSH surfaces.
- **Advocate:** independent operator, consultant, or small-agency engineer who wants durable client-scoped context.
- **Potential buyer and payer:** an independent operator, paying in host cost, setup time, attention, and delivery risk. Today only the owner or a separately permissioned operator can adopt; there is no public usage grant or purchase transaction.
- **Painful trigger:** repeated sessions lose context, a raw host drifts, or a platform adds more control-plane work than one operator wants.
- **Old alternative:** raw VPS plus scripts; environment/thin-remote tools; remote-dev platforms; ephemeral agent runtimes.
- **Intermediaries cut:** browser IDE as center, hosted workspace control plane, and runtime live skill fetch.
- **Intermediaries kept:** operator-owned cloud host, Docker, and Tailscale add real execution, packaging, and private-network value.
- **Wedge question:** “Can the box prove its clients, skills, runtime state, logs, pressure, and safety gates without me babysitting it?”
- **Offer today:** inspectable reference source plus dated operator proof; no public usage grant, paid plan, or hosted service.
- **Retained behavior:** continue using focused client context and proof commands across agent sessions. This is a product hypothesis; aggregate retention is unmeasured.

## Competing acquisition systems

### System A — Meta pain-to-install funnel

**Audience hypothesis:** English-speaking independent developers, technical consultants, and small-agency engineers with lawful platform interests in Claude Code, Codex, Tailscale, Docker, tmux, remote development, and self-hosting. Do not infer sensitive traits. Exclude existing converters only if a consented audience and tested conversion event later exist.

**Creative matrix (draft only):**

| Hook | Visual | Body | CTA | Promise | Proof | Objection |
|---|---|---|---|---|---|---|
| “Your coding agent starts from zero again.” | Real terminal capture of state before/after focus | One private workstation keeps declared homes, repos, logs, and client context together. | Inspect the setup | Durable declared state | Dated first-box walkthrough | “Why not a VPS?” → repeatability and checks |
| “A solo operator does not need a workspace platform.” | Simple raw-host/platform/Skillbox category diagram | Skillbox is deliberately one operator, one Tailnet-first machine. | Compare the options | Less control-plane scope | Public manifests and decision guide | “Is it secure?” → topology, not certification |
| “Can your agent prove which client it is working for?” | Real `focus` and `doctor` output | Client overlays and checks make current context inspectable. | Inspect the captured proof | Legible current state | Commit-bound proof artifact | “Will it work for me?” → rights boundary and dated evidence |

**Destination:** GitHub README with message-matched reference-source hero and proof CTA.
**Proposed signal:** platform impressions followed by GitHub referrer visits; no centrally implemented event exists.
**Retargeting:** not available without consented tracking, an adoption endpoint, and a lawful audience.
**Decision:** reject for first execution. There is no public usage grant, paid artifact, pixel, consent path, audience baseline, or verified conversion event. No budget is proposed until those gates exist.

### System B — Owned search/content funnel

**Problem and intent cluster:**

| Intent | Query family | Destination | CTA |
|---|---|---|---|
| Awareness | private coding-agent workstation; durable Claude/Codex home | README definition and problem/solution | Inspect the proof |
| Consideration | raw VPS vs DevPod vs Coder for coding agents | `docs/private-coding-agent-workstation.md` | Compare the categories |
| Decision | private Claude Code server; Tailscale coding-agent box | README proof, license boundary, FAQ | Resolve permission |

**Pillar:** GitHub README, because it is the actual public product landing surface.
**First complete supporting asset:** the private coding-agent workstation decision guide.
**Internal path:** article → dated first-box proof → README rights boundary; README → article and proof.
**Distribution:** an approval-queued critique post may link the same decision guide after community-rule and human review; curator listings remain deferred until reuse eligibility is resolved.
**Measurement:** monthly GitHub traffic/referrer snapshots, article/search query checks, and linked technical feedback. Install/activation are not valid campaign conversions while usage rights are unresolved. No invasive runtime beacon.

### System C — Social/community qualified-critique funnel

**Delivered first surface:** one Ask HN critique draft. Reddit and curator/list
surfaces are deferred pending separate message-match, rule, eligibility, and
approval review.

**Native content arc:**

1. Pain-first post: “I wanted one private machine whose agent context survived the session.”
2. Show the real runtime declaration and dated first-box output.
3. Explain why the project is not a browser IDE, sandbox, or team control plane.
4. Invite technical criticism of the decision guide, not praise or testimonial language.

**Draft Ask HN title:** `Ask HN: What proof would you require from a durable coding-agent workstation?`.

**Draft opening:**

> I kept rebuilding the same agent workstation state: homes, repos, client context, services, and checks. Skillbox is the deliberately narrow result—one operator, one Tailnet-first Linux box, no hosted workspace control plane. The README includes a dated first-box proof and an honest raw-VPS/DevPod/Coder decision guide. It is source-available rather than OSI-licensed, and there are no customer testimonials or paid plan.

**Approval boundary:** a human must review community rules, current repository behavior, licensing language, and every submission. No posting or outreach is authorized.
**Observed campaign path—not adoption conversion:** post impression →
repository-level visit/referrer proxy → linked qualified critique → explicit
request for usage rights. The experiment stops here. A visit does not prove
inspection; critique does not prove demand; a permission request does not prove
adoption.
**Follow-up:** answer technical questions with source links; never convert comments into testimonials without the field-report permission contract.

## Funnel scoring and selection

Scale: 0–100 on buyer fit, evidence, speed-to-signal, conversion coherence, owned compounding, cost/risk, and measurability. Weighted equally for route selection; the raw total is out of 700.

| System | Buyer fit | Evidence | Speed | Conversion | Compounding | Cost/risk | Measurability | Total |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Meta pain-to-install | 46 | 35 | 55 | 28 | 20 | 22 | 25 | 231 |
| Owned search/content | 88 | 84 | 76 | 38 | 92 | 91 | 45 | 514 |
| Social/community critique | 84 | 83 | 82 | 63 | 64 | 77 | 70 | 523 |

The route score reflects the current inspection/demand-signal job, not public
adoption. Social critique wins narrowly because a linked objection or explicit
rights request is directly observable; owned search compounds but cannot verify
reading or demand by itself.

**Selected delivery system:** social/community qualified-critique and
demand-signal experiment, supported by the owned README and decision guide.
Repository research identifies a human-approved Ask HN inspection
experiment as the first signal test, while comparison search is a support asset
first and acquisition asset second. Adoption remains blocked and acquisition
unproven. Inspection distribution requires outbound authority, current
community-rule review, dated claims, and human approval; it does not require a
reuse grant. Meta remains gated.

## Measurement contract

| Stage | Decision question | Evidence source | Observation | Current state |
|---|---|---|---|---|
| Impression | Does problem-led distribution reach the platform audience? | platform post metrics | pre-post, 24-hour, and 7-day snapshots | manual access not authorized; blank runbook exists |
| Inspection proxy | Is repository traffic associated with the source and window? | GitHub referrer observations | source-matched snapshots | does not verify reading |
| Qualified critique | Does a non-owner name a concrete claim, control, proof gap, or missing check? | linked reply | link, theme, and count | no observations yet |
| Demand signal | Does someone explicitly request permission or a public usage grant? | linked request or owner-controlled aggregate | source and count | no public flow; none observed |

The [channel asset](social-community-launch-pack.md) defines these observations,
provides a blank capture sheet, and marks unavailable metrics as unavailable
rather than zero. Do not calculate an impression-to-visit rate unless source,
window, and denominator match.

### Deferred adoption validation

| Stage | Evidence required | Current state |
|---|---|---|
| Adoption | Permissioned first-box completion after a public usage grant and release-candidate proof | blocked; no adoption evidence |
| Retention | Later dated observation after a named restart or workspace-container rebuild showing continued focus/proof-command use | blocked; no retention evidence |

No adoption or retention rate may be reported without defined populations and
denominators. Neither stage is an extension of the current critique experiment.

Decision rules:

1. Do not add telemetry until a privacy/consent contract and an action tied to each event exist.
2. Review the same small query/referrer set monthly; unknown stays unknown.
3. Keep every social/community draft in the approval queue unless outbound authority, current community-rule review, dated claims, and human approval all exist.
4. Do not advance Meta until a measurable, consented conversion and economically meaningful offer exist.

## Distribution and adoption gates

**Inspection distribution:** outbound authority, an inspection-only CTA, dated
and commit-bound claims, current community-rule review, and human approval are
all required. This campaign has no outbound authority, so all drafts remain
unpublished.

**Adoption:** remains blocked until the owner supplies an explicit public usage
grant and the clean-clone walkthrough is rerun at the release-candidate SHA.
The compatible Docker-host and Tailnet prerequisites must be rechecked in that
release proof.

## Initial marketing quality score

| Dimension | Score / 1000 | Weight | Weighted score |
|---|---:|---:|---:|
| Buyer/payer/power map | 840 | 180 | 151.2 |
| Evidence and claim integrity | 870 | 180 | 156.6 |
| Landing clarity/message match | 790 | 160 | 126.4 |
| Funnel coherence | 780 | 160 | 124.8 |
| Conversion trust/friction | 760 | 140 | 106.4 |
| Distribution asset specificity | 790 | 100 | 79.0 |
| Measurement readiness | 620 | 80 | 49.6 |
| **Total** |  | **1000** | **794.0** |

Largest initial weighted losses: landing clarity 33.6; funnel coherence 35.2; conversion trust 33.6. The Oracle rounds must attack current artifact bytes and raise the score through file changes, not agreement.

## Round 1 score

| Dimension | Score / 1000 | Weight | Weighted score |
|---|---:|---:|---:|
| Buyer/payer/power map | 875 | 180 | 157.5 |
| Evidence and claim integrity | 920 | 180 | 165.6 |
| Landing clarity/message match | 810 | 160 | 129.6 |
| Funnel coherence | 720 | 160 | 115.2 |
| Conversion trust/friction | 800 | 140 | 112.0 |
| Distribution asset specificity | 810 | 100 | 81.0 |
| Measurement readiness | 640 | 80 | 51.2 |
| **Total** |  | **1000** | **812.1** |

Largest weighted losses after round 1: funnel coherence 44.8; landing clarity 30.4; measurement readiness 28.8. The lower funnel score is intentional: the rights blocker is now explicit rather than averaged away.

## Round 2 score

| Dimension | Score / 1000 | Weight | Weighted score |
|---|---:|---:|---:|
| Buyer/payer/power map | 900 | 180 | 162.0 |
| Evidence and claim integrity | 910 | 180 | 163.8 |
| Landing clarity/message match | 870 | 160 | 139.2 |
| Funnel coherence | 740 | 160 | 118.4 |
| Conversion trust/friction | 850 | 140 | 119.0 |
| Distribution asset specificity | 700 | 100 | 70.0 |
| Measurement readiness | 600 | 80 | 48.0 |
| **Total** |  | **1000** | **820.4** |

Largest weighted losses after round 2: funnel coherence 41.6; measurement
readiness 32.0; distribution asset specificity 30.0.

## Round 3 score

| Dimension | Score / 1000 | Weight | Weighted score |
|---|---:|---:|---:|
| Buyer/payer/power map | 900 | 180 | 162.0 |
| Evidence and claim integrity | 910 | 180 | 163.8 |
| Landing clarity/message match | 870 | 160 | 139.2 |
| Funnel coherence | 850 | 160 | 136.0 |
| Conversion trust/friction | 850 | 140 | 119.0 |
| Distribution asset specificity | 820 | 100 | 82.0 |
| Measurement readiness | 700 | 80 | 56.0 |
| **Total** |  | **1000** | **858.0** |

Largest weighted losses after round 3: funnel coherence 24.0; measurement
readiness 24.0; conversion trust/friction 21.0. Round 4 accepted the content
threshold and required only destination, unavailable-observation, and acceptance
record corrections.
