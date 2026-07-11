<div align="center">

# skillbox

**A private, single-tenant Tailnet box for you and your coding agents.**

Thin, self-hosted, Docker-based, with durable runtime state under `.skillbox-state/` and client-scoped overlays.

![runtime](https://img.shields.io/badge/runtime-Docker-2496ED?style=flat-square&logo=docker&logoColor=white)
![access](https://img.shields.io/badge/access-Tailscale-242424?style=flat-square&logo=tailscale&logoColor=white)
![shape](https://img.shields.io/badge/shape-thin%20starter-6E7781?style=flat-square)
![doctor](https://img.shields.io/badge/doctor-manifest%20checks-2ea44f?style=flat-square)

</div>

## TL;DR

Most remote dev setups overshoot the need. You want one private box that feels
like a real computer: one primary workspace container, your own Claude or Codex
home directories, a broader repo universe, and a way to activate the right
client context without standing up a full hosted workspace control plane.

`skillbox` gives you a cloneable starter for a Tailnet-first dev box with a
Docker workspace, durable state under `.skillbox-state/`, client overlays,
explicit runtime graphs, and compact operator commands.

### Why Use `skillbox`?

| Need | `skillbox` answer |
|---|---|
| Private access without public SSH exposure | Managed boxes default to `tailnet_only`: public SSH is temporary during bootstrap/enroll, then host SSH and the DigitalOcean firewall are locked to Tailnet access |
| A workspace that feels like a narrowed local setup | One bind-mounted `/workspace`, plus durable state from `SKILLBOX_STATE_ROOT` mounted into `/workspace/logs`, `/workspace/workspace/clients`, `/home/sandbox`, and optional `/monoserver` |
| A sane way to let the box grow over time | `workspace/runtime.yaml` plus `.env-manager/manage.py` manage the core machine plus client-specific repos, artifacts, installed skills, logs, and checks |
| Service graphs that do not devolve into shell folklore | Declared `depends_on` edges let `up`, `down`, and `restart` expand and order service graphs automatically |
| Live drift detection and auto-healing | The pulse daemon monitors services on a fixed interval, auto-restarts crashes, and appends human-readable events to `logs/runtime/runtime.log` |
| Runtime history plus durable work notes | `focus` surfaces recent runtime activity, while `skillbox_session_*` tools and `cm` carry longer-lived work context |
| One-command client activation | `focus` syncs, bootstraps, starts services, collects live state, and writes enriched agent context in a single pass |
| Fleet management from the operator machine | The operator MCP server provisions DO droplets, enrolls Tailscale, and runs commands on remote boxes as native agent tools |
| Reproducible default skills | `skill-repos.yaml` declares GitHub repos and local paths; `sync` clones and filtered-installs skills |
| Confidence that docs/config/runtime still match | `04-reconcile.py` powers `make render` and the outer `make doctor` path, while `make dev-sanity` validates the live runtime internals |
| Minimal surface area | No multi-tenant control plane, no hosted dependency, no hidden sibling repo requirement for packaging |

## Why `skillbox` Exists

`skillbox` is aimed at one narrow gap: one private machine that feels like a real
computer for one operator and their agents, with persistent homes, repo overlays,
explicit runtime state, and low operational ceremony.

For the deeper thesis, see [docs/VISION.md](docs/VISION.md).

## Quick Start

```bash
git clone https://github.com/build000r/skillbox.git
cd skillbox
make bootstrap-env
make render
make runtime-sync
make doctor
make dev-sanity
make build
make up
make shell
```

For a captured zero-to-focused-client walkthrough with real expected output, see
[examples/first-box-demo.md](examples/first-box-demo.md).

## When Not To Use `skillbox`

| Need | Use something else when... |
|---|---|
| Browser IDE product | The core experience needs to live in the browser. |
| Multi-user workspace fleets | You need tenancy, RBAC, policy layers, audit controls, or a hosted control plane. |
| Untrusted-code sandboxing | Isolation and ephemeral execution are the main job. |
| Environment management only | You just need reproducible packages or shell environments. |
| Hosted SaaS ergonomics | You do not want to operate the host, Docker, and private access model yourself. |

`skillbox` is best when the problem is narrower: one operator-owned machine that
should feel durable, legible, and agent-friendly.

## Command Surface

| Surface | Use it for | Details |
|---|---|---|
| `make` targets | Operator-friendly host commands for bootstrap, render, doctor, runtime sync/status, build, box lifecycle, and shell access. | [API reference](docs/API_REFERENCE.md) |
| `.env-manager/manage.py` | Runtime graph, client focus, services, logs, skill visibility, search, snapshots, and MCP rendering. | [API reference](docs/API_REFERENCE.md) |
| `scripts/box.py` | DigitalOcean/Tailscale box lifecycle, posture proof, status, and recovery-oriented box operations. | [API reference](docs/API_REFERENCE.md) |
| `scripts/sbp` / `scripts/sbo` | Skill policy, overlays, MCP visibility, wrapper ergonomics, and repo-local skill decisions. | [API reference](docs/API_REFERENCE.md) |
| MCP tools | In-box and operator tools exposed to coding agents with structured outputs and server-side safety gates. | [API reference](docs/API_REFERENCE.md) |

## Documentation Map

| Topic | Start here |
|---|---|
| Architecture and data flow | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Runtime graph, profiles, tasks, services, and env files | [docs/runtime-graph.md](docs/runtime-graph.md) |
| Client init, focus, first-box, projection, opening, and publish flows | [docs/clients.md](docs/clients.md) |
| Skill repos, lockfiles, visibility, forge, and distribution | [docs/skills.md](docs/skills.md) |
| Focus, pulse, workers, swimmers, fleet operations, and MCP operations | [docs/operations.md](docs/operations.md) |
| Troubleshooting and known limitations | [docs/troubleshooting.md](docs/troubleshooting.md) |
| Product stance and common questions | [docs/faq.md](docs/faq.md) |
| Tailnet-only lifecycle and recovery | [docs/tailnet-only-lifecycle.md](docs/tailnet-only-lifecycle.md) |
| Tailnet ingress | [docs/tailnet-ingress.md](docs/tailnet-ingress.md) |
| Vision and market map | [docs/VISION.md](docs/VISION.md) |
| Full command/API reference | [docs/API_REFERENCE.md](docs/API_REFERENCE.md) |

## Quick Example

Moved to [docs/clients.md#quick-start](docs/clients.md#quick-start) and the captured walkthrough at [examples/first-box-demo.md](examples/first-box-demo.md).

## Focus

Moved to [docs/operations.md#focus](docs/operations.md#focus).

## Skillbox Forge

Moved to [docs/skills.md#skillbox-forge](docs/skills.md#skillbox-forge).

## Local Runtime Profiles

Moved to [docs/runtime-graph.md#local-runtime-profiles](docs/runtime-graph.md#local-runtime-profiles).

## Pulse Daemon

Moved to [docs/operations.md#pulse-daemon](docs/operations.md#pulse-daemon).

## Worker Runtime Broker

Moved to [docs/operations.md#worker-runtime-broker](docs/operations.md#worker-runtime-broker).

## Swimmers Overlay

Moved to [docs/operations.md#swimmers-overlay](docs/operations.md#swimmers-overlay).

## Agent Context

Moved to [docs/operations.md#agent-context](docs/operations.md#agent-context).

## Fleet Management

Moved to [docs/operations.md#fleet-management](docs/operations.md#fleet-management).

## Design Stance

Moved to [docs/faq.md#design-stance](docs/faq.md#design-stance).

## Comparison

Moved to [docs/faq.md#comparison](docs/faq.md#comparison).

## Installation

Moved to [docs/clients.md#installation](docs/clients.md#installation).

## Command Reference

Moved to [docs/operations.md#command-reference](docs/operations.md#command-reference) and [docs/API_REFERENCE.md](docs/API_REFERENCE.md).

## Configuration

Moved to [docs/runtime-graph.md](docs/runtime-graph.md), [docs/clients.md](docs/clients.md), and [docs/skills.md](docs/skills.md).

## Architecture

Moved to [docs/runtime-graph.md#architecture](docs/runtime-graph.md#architecture) and expanded in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## MCP Integration

Moved to [docs/operations.md#mcp-integration](docs/operations.md#mcp-integration).

## Troubleshooting

Moved to [docs/troubleshooting.md#troubleshooting](docs/troubleshooting.md#troubleshooting).

## Limitations

Moved to [docs/troubleshooting.md#limitations](docs/troubleshooting.md#limitations).

## FAQ

Moved to [docs/faq.md#faq](docs/faq.md#faq).

## About Contributions

> *About Contributions:* Please don't take this the wrong way, but I do not accept outside contributions for any of my projects. I simply don't have the mental bandwidth to review anything, and it's my name on the thing, so I'm responsible for any problems it causes; thus, the risk-reward is highly asymmetric from my perspective. I'd also have to worry about other "stakeholders," which seems unwise for tools I mostly make for myself for free. Feel free to submit issues, and even PRs if you want to illustrate a proposed fix, but know I won't merge them directly. Instead, I'll have Claude or Codex review submissions via `gh` and independently decide whether and how to address them. Bug reports in particular are welcome. Sorry if this offends, but I want to avoid wasted time and hurt feelings. I understand this isn't in sync with the prevailing open-source ethos that seeks community contributions, but it's the only way I can move at this velocity and keep my sanity.

## License

All rights reserved. No license file is included and no OSI license is granted yet (operator decision, 2026-06-21): the source is published for reading and reference, but you may not redistribute or reuse it without permission.
