# Dueling Idea Wizards Report: skillbox × ui.sh architecture

## Executive Summary

Two different models (Claude Code and Codex/gpt-5.3-codex xhigh) were given the same question — *should skillbox adopt ui.sh's remote HTTP MCP + stub-SKILL.md architecture?* — ideated independently, cross-scored adversarially, then reacted to each other's scoring under pressure. Despite radically different entry points (CC framed it as a consumption/runtime problem; Codex framed it as a transport/governance problem), both agents converged on the same final sequence in the reveal phase.

**The consensus answer:** do NOT adopt the remote transport. DO adopt the router/lazy-loading pattern locally via an MCP resource layer on the existing `mcp_server.py`. Keep the git-repo + lockfile trust anchor. Treat any future remote source as sync-time-only, policy-gated, operator-owned optionality — never runtime live fetch.

**Top 3 consensus winners** (scored 700+ by both agents, survived reveal):
1. **Local skill runtime mediation layer** — add `resources/list` + `resources/read` to `mcp_server.py`, serve `skillbox://skills/<name>/<path>` from installed skill dirs
2. **Reject live remote skill fetch on trust grounds** — skill content = high-privilege agent instructions; the operator must control update timing
3. **Narrow-scope `skillbox://` URI contract** — start with skills only, expand only after proven

## Methodology

- **Agents:** cc (Claude Code, Sonnet-class in pane 0) + cod (gpt-5.3-codex xhigh in pane 1) via NTM swarm
- **Phases:** study → ideate (10 → 5 with ultrathink) → cross-score (0–1000) → reveal → react → synthesize
- **Ground truth read:** skillbox/README.md, docs/VISION.md, runtime_ops.py, validation.py, skill-repos.yaml, `_shared/scripts/resolve_context.py`, `mcp_server.py`, client overlay structure
- **Artifacts:** WIZARD_IDEAS_{CC,COD}.md, WIZARD_SCORES_{CC_ON_COD,COD_ON_CC}.md, WIZARD_REACTIONS_{CC,COD}.md

## Score Matrix

| Idea | Origin | Self-rank | Self-est | Cross-score | Post-reveal verdict |
|---|---|---:|---:|---:|---|
| Missing skill runtime layer (local MCP resource mediation) | CC | 1 | 780 | **872** | **CONSENSUS WIN** |
| Reject remote live fetch (trust-model argument) | CC | 2 | 870 | **934** | **CONSENSUS WIN** — CC reordered to #1 after reveal |
| `skillbox://` intra-box URI scheme | CC | 3 | 680 | 808 | Consensus but **start narrow** — CC self-corrected down to 650-680 |
| Usage observability via mediated reads | CC | 4 | 720 | 781 | Consensus — read telemetry ≠ effectiveness telemetry (fidelity caveat) |
| Stub/content decoupling | CC | 5 | 550 | 562 | **Defer** — both agree premature until runtime layer proven |
| Hybrid cache-first remote source | COD | 1 | 780 | **430** | **CONTESTED** — Codex defended legitimate niche, but demoted from #1 to #4 |
| Router pattern locally (keep transport local) | COD | 2 | 780 | 590 | Merged with CC #1 — same idea, less detail |
| Signed bundle distribution channel | COD | 3 | 620 | **260** | **KILLED as replacement** — violates Design Stance #5; survives as complement only |
| Private-first policy-gated remote | COD | 4 | 650 | 370 | Conditional guardrail — valuable only if remote is ever added |
| Two-lane stable/live | COD | 5 | 500 | **220** | **KILLED** — assumes multi-stakeholder model skillbox rejects |

## Consensus Winners (both agents, ≥700 after reveal)

### 1. Local skill runtime mediation layer (the anchor move)
**What:** Add `resources/list` and `resources/read` to the existing `mcp_server.py` (currently tools-only). Serve `skillbox://skills/<name>/<path>` directly from the installed skill directory under `.skillbox-state/home/.claude/skills/<name>/`.

**Why it wins:** It captures the only genuinely good property of ui.sh's architecture (mediated lazy loading) without any of the costs (network dependency, bearer tokens, live content fetch from third parties, VISION.md violations). The MCP server already exists, already serves tools — adding a resource surface is the natural extension.

**Revised cost estimate after reveal:** 300–500 lines (protocol plumbing + resolution logic + error handling + tests), not CC's original "100–150 lines." This is a medium feature, not a weekend patch.

**Sequencing:** prototype first, validate agent compliance with router instructions in a real session, *then* standardize conventions and doctor checks.

### 2. Reject live remote skill fetch on trust grounds
**What:** Document explicitly that skillbox will never adopt runtime live fetch of skill content from third-party MCP servers. Skill content = agent instructions = high-privilege behavior control over a workspace with source code, infra credentials, and MCP tools. The operator must control update timing via explicit `make runtime-sync`.

**Why it wins:** Costs nothing to implement, prevents an irreversible strategic mistake, directly preserves five of six stated values (single-tenant, durable, explicit, private, honest scope). Codex scored this 934 — the highest score in the entire duel. CC post-reveal agreed this should have been ranked #1, not #2.

**Nuance from reveal:** not "remote = binary bad." The distinction is **live runtime fetch** (categorically rejected) vs **sync-time pinned ingestion** (legitimate optionality — see contested idea below).

### 3. Narrow `skillbox://` URI scheme (skills only, day one)
**What:** Register a `skillbox://` resource provider in the MCP server. Initial surface: `skillbox://skills/<name>/` and `skillbox://skills/<name>/<path>` only. Defer `skillbox://context/*`, `skillbox://runtime/*`, `skillbox://plans/*` until the skills surface has proven itself.

**Why narrow:** CC initially proposed a broad URI space. Codex flagged that `_shared/scripts/resolve_context.py` already resolves client context via env var + cwd_match + overlay scan. CC verified this and conceded the overlap. A URI scheme becomes a **contract** — once skills reference `skillbox://` URIs, changing the scheme breaks them. Scope discipline matters.

### 4. Usage observability from mediated reads
**What:** Every MCP resource read emits a `skill.read` event to `logs/runtime/runtime.log` — the same pipeline that `focus`/`pulse`/`status` already surface. Falls out for free from #1.

**Fidelity caveat (Codex's catch, CC conceded):** read telemetry ≠ effectiveness telemetry. It tells you what was loaded, not what was helpful. Exploratory reads, retries, and partial reads add noise. Useful as a coarse signal ("which skills are ever touched?") but not as a curation-decision source without complementary data.

### 5. Router SKILL.md as an authoring convention (not installation mode)
**What:** Treat SKILL.md files as routers pointing at `skillbox://skills/<name>/<path>` subpages, rather than monoliths. Enforce via doctor lint (max line count + required router structure once adopted).

**Not yet:** making stubs the default installation mode (CC's idea #5). Both agents agree this is premature until runtime layer reliability is proven. Prototype + validate first.

## Contested After Reveal (live disagreement with real reasons on both sides)

### Optional sync-time remote ingestion channel (Codex #1, rebutted)
CC scored this 430 with "solves a nonexistent problem." Codex's post-reveal rebuttal was the strongest defense in the entire duel and deserves to land:

> #1 is not "remote runtime fetch"; it's "operator-controlled import channel." Fetch only during sync, materialize to local files, lock with digests, agents consume local snapshot only. Remote origin, local truth.

**Codex's four concrete scenarios:**
1. Cross-box fleet consistency for one operator (no manual repo choreography across N boxes)
2. External high-signal skills NOT published as git repos (proprietary feeds, ui.sh-like services)
3. Operator-owned private endpoint distributing centrally authored skills across clients
4. Air-gapped promotion flow (remote ingest in one environment, pinned-cached into another)

**Verdict:** Codex is right that CC's "nonexistent problem" framing is too narrow. The scenarios are real. But Codex also conceded this should NOT be priority #1 — it comes after the runtime layer and only as optional, bounded, sync-time-only, policy-gated infrastructure. **Status: deferred optionality, not rejected. Re-evaluate after the local runtime layer ships.**

### URI scope breadth
CC wanted `skillbox://context/*`, `skillbox://runtime/*`, `skillbox://plans/*`, `skillbox://sessions/*`, `skillbox://log/*`. Codex pointed out overlap with existing mechanisms (`context.yaml`, `resolve_context.py`, `runtime.log`). **Consensus in reveal:** start narrow (skills only); broader URI scopes can be added incrementally *after* the initial surface proves useful, and only where they don't duplicate existing resolution paths.

## Killed Ideas (mutual agreement after reveal)

- **Pure adoption of ui.sh's remote architecture.** Violates 5 of 6 VISION.md values, introduces permanent network dependency on every skill invocation, inverts the trust model for agent instructions.
- **Signed bundles as *replacement* for git-repo skills.** Directly contradicts Design Stance #5. Codex conceded this was an argument-rigor miss; defended it only as a *complement* for skills where source repos are unavailable.
- **System-level two-lane (stable/live) architecture.** Assumes a multi-stakeholder update model skillbox explicitly rejects. The mechanism for "pinned vs tracking" already exists at the source entry level (`ref: main` vs lockfile-pinned commit SHA).
- **Stub-as-default installation mode, now.** Both agents agree: research direction, not near-term default. Migration should be earned, not imposed.

## Blind-Spot Idea (surfaced only through the adversarial exchange)

### Skill ABI / Compatibility Contract (Codex, reveal phase)
Neither agent raised this in initial ideation. It emerged only after Codex was forced to defend its governance-heavy framing while integrating CC's runtime framing.

**What:** A versioned contract for skill packages/resources:
- Required metadata (id, version, capability tags, dependencies)
- Router declaration format (when present)
- Allowed URI scopes per ABI version
- Compatibility matrix with MCP resource server version
- Deterministic digesting rules for lockfile reproducibility

**Why this bridges both camps:**
- *Install/governance camp:* lockfiles record ABI version; doctor rejects incompatible imports early with structured errors
- *Runtime/consumption camp:* resource server validates router/subresource correctness; operators get clear upgrade safety signals

**Why it's valuable:** It's the missing bridge between CC's "runtime mediation" framing and Codex's "deterministic governance" framing. Without an ABI, both evolve independently and eventually collide. With an ABI, skill authoring becomes a stable target that both the install-time and read-time surfaces can validate against.

**Status:** Needs a dedicated design pass. Lower priority than shipping the MCP resource layer itself, but should land before the URI scheme expands beyond skills.

## Meta-Analysis

### Model bias signatures

**CC (Claude Code):**
- Deep diagnostic framing ("skillbox has no skill runtime layer — that's the real gap")
- Strong on value-alignment arguments (trust model, VISION.md references)
- Self-critical under pressure (admitted "my five ideas are really two ideas + three implications")
- Weaker on concrete implementation detail (underestimated MCP resource impl 3–5×)
- Scored harsh (max 590 given to any Codex idea)

**Codex (gpt-5.3-codex xhigh):**
- Strong on schema-level implementation detail (config types, lockfile fields, validation flow)
- Governance/transport lens by default (source types, sync policies, policy gates)
- Generous scoring (min 562, max 934)
- Conceded directionally (reordered his own list to match CC's anchor) while defending his niche-optionality argument (4 concrete scenarios)
- Surfaced the only genuinely novel idea (Skill ABI) in the post-reveal phase

### What convergence revealed

Both agents arrived at the same implementation sequence via different paths:

| Step | CC's framing | Codex's framing |
|---|---|---|
| 1 | Keep git/file/lock as trust anchor | Keep current file/git/lock model as trust anchor |
| 2 | Add MCP resource layer for local skill content | Prototype local resource runtime for installed skills |
| 3 | Build `skillbox://` URI scheme | Add narrow `skillbox://` scope + usage logging |
| 4 | Observability falls out of resource layer | *(same, merged)* |
| 5 | Defer stubs until proven | Defer stub-default until runtime layer proves reliability |
| — | *(not in initial list)* | Treat remote delivery as optional, sync-time, pinned |

The triangulation signal is strong: **two models with different biases reached the same architectural answer.** CC added the "why" (trust model, diagnosis); Codex added the "what else" (remote optionality, ABI). Neither is complete without the other.

### What adversarial pressure surfaced that neither had alone

1. **The Skill ABI idea** — visible only after both framings collided
2. **CC's cost underestimate** (100-150 → 300-500 lines) — exposed by Codex's "mcp_server is tool-only today" reality check
3. **Codex's missing engagement with Design Stance #5** — exposed by CC's direct citation
4. **CC's "five ideas are really two" self-critique** — only landed after seeing his own list reflected through Codex's more generous scoring
5. **The "what actually hurts today?" gap** — CC's sharpest self-critique in the reveal: neither agent grounded the analysis in observed operator pain. Without knowing the actual felt problem (context bloat? staleness? authoring friction?), both proposals solve inferred rather than reported problems.

## Recommended Next Steps (concrete, ranked)

### Priority 1 — Ship the trust-model commitment (zero code)
Add a `docs/DESIGN_STANCES.md` entry (or extend VISION.md) that explicitly rejects live remote fetch of skill content and defines the "sync-time-only, pinned, cached, operator-owned" constraint for any future remote optionality. Cost: half an hour. Prevents an entire class of future mistakes.

### Priority 2 — Prototype the MCP resource layer (medium feature)
1. Extend `mcp_server.py` capabilities to declare `"resources": {"listChanged": false}`
2. Implement `resources/list` and `resources/read` dispatch
3. Wire `skillbox://skills/<name>/` and `skillbox://skills/<name>/<path>` to resolve under `.skillbox-state/home/.claude/skills/<name>/`
4. Write ONE test skill with a router SKILL.md that references `skillbox://skills/<test>/<subpage>`
5. **Run a real agent session against it and verify the agent actually fetches sub-resources.** This is the critical validation — if agent compliance isn't reliable, the entire pattern needs rethinking.
6. Only after step 5 succeeds: write convention docs, add doctor lint for router structure

Estimate: 300–500 lines across the server + a test skill + conventions doc. ~1–2 focused sessions if step 5 passes cleanly; indefinitely longer if it doesn't.

### Priority 3 — Ground analysis in observed operator pain (before more architecture)
Before expanding beyond the skills-only URI surface, instrument one week of real skillbox sessions and answer: what's the actual top pain? Context-window bloat at startup? Skill drift between boxes? Authoring friction? Curation difficulty? The runtime layer solves some of these; if the real pain is something else, it solves the wrong thing.

### Priority 4 — Usage observability hook (trivial after P2)
Once the resource layer exists, add `log_runtime_event("skill.read", …)` to the resource handler. Surfaces in `focus` / `status` / runtime log without new infrastructure. Do NOT build interpretation tooling on top until fidelity is understood (read ≠ effectiveness).

### Priority 5 — Skill ABI design pass (after resource layer ships)
Draft a versioned metadata schema (`skill.yaml` or frontmatter in SKILL.md) covering: id, version, ABI level, router declaration format, allowed URI scopes, dependency tags. Wire it into the lockfile and doctor. Blocks expansion of the URI scheme beyond skills.

### Priority 6 — Defer: everything else
- Broader URI scheme (`skillbox://context/*`, `runtime/*`, `plans/*`)
- Stub-as-default installation mode
- Optional sync-time remote ingest channel
- Signed bundles as a complement source type

These are real options, but all depend on P2 shipping and proving itself. Re-evaluate quarterly.

---

**Bottom line:** ui.sh's architecture is not wrong for ui.sh — it's wrong for skillbox. But the *pattern* inside it (router SKILL.md + mediated lazy loading + usage observability) is valuable, and skillbox can capture it locally via an MCP resource extension. That's the whole play. Everything else either follows from it, depends on it, or should be explicitly deferred.
