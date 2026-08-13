# Product Marketing Context

*Last updated: 2026-08-13; evidence baseline: `dd5ffc5052a71e212eff8bfad900b5ac511748f0`*

## Product Overview

**One-liner:** A durable, private Linux workstation for one operator and their coding agents.

**Category:** Private coding-agent workstation / self-hosted developer infrastructure.

**Product type:** Source-available, operator-run CLI and runtime configuration.

**Business model:** No paid plan, hosted service, trial, support SLA, or public usage grant is defined. The current public offer is reference source and captured operator proof.

## Target Audience

**Primary user and buyer:** An independent technical operator who runs Claude Code, Codex, or similar terminal agents and owns the workstation's time, infrastructure cost, and delivery risk.

**Secondary advocates:** Consultants or small agencies that need client-scoped overlays; trusted collaborators may use the box but are not the central buyer.

**Primary job:** Keep agent homes, repositories, logs, services, and client context durable and inspectable without adopting a hosted workspace control plane.

## Trigger, Alternatives, and Wedge

**Trigger:** The operator repeatedly reconstructs state, context, services, or skill visibility, or a raw host has become hard to reason about.

**Alternatives:** Raw VPS and scripts; environment/thin-remote tools; remote-development control planes; ephemeral agent runtimes.

**Wedge question:** Can the box prove its current clients, skills, runtime state, logs, pressure, and safety gates without operator babysitting?

## Differentiation

- one operator and one private machine by design;
- durable Claude/Codex homes, repos, logs, and client overlays;
- explicit runtime graphs, checks, and structured command output;
- Tailnet-first access posture;
- CLI-first rather than browser-first or hosted-control-plane-first.

## Objections and Boundaries

| Objection | Evidence-backed response |
|---|---|
| Why not a raw VPS? | Use a raw VPS when minimum machinery is the goal; Skillbox adds a declared state and validation model. |
| Why not Coder/Gitpod? | Those categories fit team workspaces and policy; Skillbox intentionally targets one operator. |
| Is it a secure sandbox? | No. Untrusted-code isolation is a different job. “Private” is a topology, not a certification. |
| Is it open source? | Source-available; no OSI license is granted. |
| Is there customer proof? | No permissioned customer report exists. Current proof is captured operator evidence. |

**Anti-persona:** Teams needing multi-user tenancy/RBAC, hosted support, browser-first workspaces, or ephemeral untrusted-code execution.

## Voice and Claim Safety

**Voice:** Direct, technical, bounded, inspectable.

**Use:** “one operator,” “durable,” “Tailnet-first,” “operator evidence,” “source-available.”

**Avoid:** “enterprise-ready,” “secure,” “zero trust,” “open source,” “production-proven,” “cheaper/faster/better,” customer counts, savings, or benchmarks without new evidence.

## Proof and Goals

**Proof:** `examples/first-box-demo.md`, captured 2026-07-05 at commit `e6f21b0`. It is dated operator proof, not current-main or customer proof.

**Primary campaign action:** Inspect the first-box proof and name a concrete missing trust check. This is a critique request, not adoption conversion; public installation is blocked until the owner grants usage rights.

**Current acquisition metrics:** Unknown. No acquisition/product analytics contract exists.
