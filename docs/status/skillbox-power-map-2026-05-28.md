# Skillbox Power Map - 2026-05-28

Scope: planning-only output for `skillbox-portfolio-reality-idea-plan-h1k.3`.
This artifact ranks the idea slate by value-chain proximity and first-surface fit.
This is dated evidence from 2026-05-28; later push-readiness checks should be
treated as the current source of truth.

## Starting Assumption

Skillbox is for one operator-owned private machine that supports that operator and their coding agents.
The repo's docs consistently reject hosted control planes, browser IDE centers of gravity, and multi-tenant platform sprawl.

## Primary Actor

Primary actor: the independent operator who pays with time, attention, infrastructure cost, and repo/deploy risk.

Immediate tool user: the coding agent running in or against the box.

Secondary actors:

- Trusted collaborators who SSH into the same single-tenant box.
- Client project owners who benefit from faster, safer operator work, but usually do not operate Skillbox directly.
- Future skill/distribution clients described by `plans/distribution-phase1/`, but they are not the current reliability-wave customer.

## Value Chain

```text
Operator time, money, and repo risk
  -> agent session that needs correct local truth
    -> Skillbox CLI/runtime/MCP surfaces
      -> durable private box, repos, homes, logs, clients, skills, and services
        -> downstream product work or client delivery
```

The person closest to the dollar is still the operator. The agent is not the buyer, but it is the interface that consumes most of the runtime truth. If the agent sees stale context, wrong clients, broken skill visibility, untrusted validation, or unsafe infra commands, the operator pays immediately in babysitting time and delivery risk.

## Intermediaries To Challenge

| Intermediary | Useful value | Why it should not own the center |
|---|---|---|
| Browser IDEs | Familiar remote editing UI | Skillbox's value is durable private agent work, not a browser workspace product. |
| Hosted remote-dev control planes | Team policy, RBAC, fleet management | Current wedge is one private box; platform ceremony is the wrong tradeoff. |
| Generic sandbox runtimes | Isolation and ephemeral execution | Skillbox optimizes durable state, persistent logs, and private repo overlays. |
| Live remote skill fetch | Centralized updates | Skill content controls agent behavior, so runtime live fetch breaks the local lockfile trust model. |
| Manual human babysitting | Can recover from drift by inspection | The product exists to make the box legible enough that the operator does not keep re-deriving state manually. |

## Wedge Question

Can I ask an agent to work on this repo and trust that the box can prove its current clients, skills, runtime state, logs, pressure, and safety gates without me babysitting it?

If the answer is no, new platform breadth does not matter yet. If the answer is yes, Skillbox becomes leverage for every downstream repo and client project.

## First-Surface Decision

First surface: CLI-first with structured JSON and inspectable artifact files.

Evidence:

- `README.md`, `AGENTS.md`, and `Makefile` define command surfaces first: `manage.py`, `make doctor`, `make dev-sanity`, `make runtime-*`, `scripts/box.py`, and `scripts/sbp`.
- `python3 .env-manager/manage.py --help` exposes the runtime contract as subcommands with `--format json` on the important inspection paths.
- `mcp-audit` and operator MCP tools are adapters over the same runtime/fleet concepts, not the only product surface.
- README limitations explicitly say the API and web surfaces are inspection stubs, not a full UI.

MCP remains important as an agent adapter where typed tools help, but it should wrap or mirror CLI truth. A hosted UI or browser app is not justified by the current power map.

## Reranked Idea Slate

| Rank | Idea | Power-map reason | Bead disposition |
|---:|---|---|---|
| 1 | Disk-safe validation trust lane | Directly protects operator time and downstream delivery; every other claim depends on trustworthy proof. | Covered by `skillbox-smart-20260518-validation-trust-gkn`; do not duplicate. |
| 2 | Active-client expectation repair | The `personal` client gap blocks the advertised first-box/focus path and confuses the agent's runtime truth. | h1k.5 should mint a direct implementation Bead if accepted. |
| 3 | Pulse daemon regression proof packet | Continuous observation is closest to the daily promise: the box should notice and explain drift without a manual re-triage. | h1k.5 should mint if the dirty pulse work is intended to land. |
| 4 | MCP parity cleanup and policy | Agents consume MCP surfaces; parity drift creates false confidence across Claude/Codex sessions. | h1k.5 should mint a direct implementation Bead if accepted. |
| 5 | Standard runtime evidence packet | Converts the h1k.1 manual proof weave into a repeatable operator/agent contract. | h1k.5 should mint if scope allows. |
| 6 | Box lifecycle dry-run drill | Infra safety is close to operator risk, but real provisioning remains side-effecting and must stay dry-run-first. | Candidate after validation trust. |
| 7 | RCH/SBH observe-first setup plan | Critical disk pressure makes offload/storage visibility valuable; mutation must remain approval-gated. | Candidate dependent on validation/disk policy. |
| 8 | Context generation drift golden tests | Agent context is central to the product, but follows the active-client and evidence-packet gaps. | Candidate implementation Bead. |
| 9 | Worker broker fake-Hermes smoke | Useful proof of agent-work delegation without real runtime launch; lower than core trust surfaces. | Candidate implementation Bead. |
| 10 | README example self-verification snippets | Keeps documentation aligned with CLI truth; likely merges with active-client repair. | Candidate or merge. |
| 11 | Fleet health parallelization baseline | Valuable performance work, but measurement belongs in h1k.4 before implementation. | Route to h1k.4. |
| 12 | Operator credential redaction pass | Potential security hardening, but needs a current source audit before minting. | Later candidate. |
| 13 | MCP resource layer for local skill content | Strong architectural idea from prior duel, but not the current dollar-adjacent pain. | Defer until reliability wave is green. |
| 14 | Skill ABI design pass | Useful after resource-layer adoption, not before current proof gaps are resolved. | Defer. |
| 15 | Distributor signed-bundle MVP | Important future product direction, but it serves future distribution clients rather than today's operator trust gap. | Separate distribution epic. |

## Accretive Top Ideas

The top five are accretive because they make the box more durable, legible, and agent-friendly without dragging it toward platform sprawl.

- Validation trust reduces wasted agent/operator loops across every repo.
- Active-client repair makes the advertised first surface truthful.
- Pulse proof turns continuous observation from a claim into a validated operating contract.
- MCP parity cleanup keeps adapter surfaces from lying to different agent runtimes.
- A runtime evidence packet makes future status checks cheaper and more repeatable.

## Positioning Guardrail

Do not prioritize distribution, hosted UI, or live-resource architecture ahead of the reliability wave. Those are real future moves, but the current power map says the closest-to-dollar work is making one private box prove its own truth quickly and safely.
