# Vision

This document captures the strategic thesis behind `skillbox`: what it is for,
what it is not for, and why it exists despite adjacent tools that already solve
other parts of the problem.

## Mission

Make one private Tailnet-connected machine feel like a real, durable
workstation for one operator and their coding agents.

## Vision

AI-assisted coding should not require either:

- a heavy remote-dev control plane
- a browser IDE as the center of gravity
- fragile ephemeral sandboxes for work that actually wants durable state

The long-term vision for `skillbox` is a default shape for private,
single-tenant agent workstations: one box with persistent homes, repo overlays,
runtime declarations, live state, and enough operator tooling to stay sane
without turning into a platform company.

## Values

### 1. Single-tenant first

`skillbox` prefers one operator, one box, one durable workspace model. It is
not trying to become a multi-user remote-dev product.

### 2. Durable state over churn

Agent homes, repo roots, logs, journal state, and client overlays should
persist. The machine should accumulate context rather than constantly starting
from zero.

### 3. Explicit systems over hidden magic

The box should describe itself with files you can inspect and validate:
runtime graphs, manifests, overlays, logs, and checks.

### 4. Private infrastructure by default

Tailscale, host SSH, and operator-owned infrastructure are a feature. The
default posture is private-by-construction, not public SaaS first.

### 5. Low ceremony over platform sprawl

If a feature drags `skillbox` toward "operate the tool before you can work," it
needs a very strong reason to exist.

### 6. Honest scope

The project should say "no" clearly when the problem is actually multi-tenant
control planes, browser IDEs, or sandbox infrastructure.

## The Wedge

The defensible wedge is not "remote development" in general.

The wedge is:

> one private machine for me and my agents, with persistent state, repo
> overlays, explicit runtime declarations, and low operational ceremony

That sits between three established buckets:

- remote-dev platforms like Coder and Gitpod
- environment and thin-remote tools like Devbox and DevPod
- agent runtimes and sandboxes like Daytona and E2B

`skillbox` exists for the case where all three buckets are close, but none is a
clean fit.

## Who It Is For

- independent operators who want one private coding box
- consultants or agencies that need client-scoped overlays on one core machine
- users who prefer SSH and Tailscale over browser IDEs
- people running Claude Code, Codex, or similar agents against durable repos
- teams small enough that "one box per operator" is simpler than standing up a platform

## Who It Is Not For

- teams that need multi-user tenancy, RBAC, and policy-heavy workspace fleets
- organizations whose primary need is secure execution of untrusted code
- products centered on browser IDE experiences
- users who only need environment packaging and not a full box model
- anyone looking for a hosted SaaS rather than operator-owned infrastructure

## Competitive Fit

| Category | Examples | What they do well | Why they do not replace `skillbox` |
|---|---|---|---|
| Raw host setup | Droplet + shell scripts | Fastest path to a custom machine | Reproducibility, drift control, and handoff are weak |
| Env and thin-remote tooling | Devbox, DevPod | Reproducible environments, remote IDE compatibility | They do not define the whole private box shape with overlays, agent homes, live context, and box-level operations |
| Remote-dev platforms | Coder, Gitpod | Browser/IDE integrations, policy layers, team workflows | More control-plane and platform weight than a single operator often needs |
| Agent runtimes | Daytona, E2B | Isolation, secure execution, ephemeral sandboxes | Optimized for runtime substrates, not durable personal workstations |

## Market Map

Axes:

- `X`: platform heft
- `Y`: agent-native focus

This is a positioning sketch, not a benchmark.

```text
10 |  .    .    .    .    .    .    .   DT    .    .  
 9 |  .    .    .    .    .   E2B   .    .    .    .  
 8 |  .   SB    .    .    .    .    .    .    .    .  
 7 |  .    .    .    .    .    .    .    .    .    .  
 6 |  .    .    .    .    .    .    .   CDR   .    .  
 5 |  .    .    .    .    .    .    .    .    .    .  
 4 |  .    .    .    .    .    .    .    .   GP    .  
 3 |  .    .    .    .   DP    .    .    .    .    .  
 2 |  .   DBX  OVS  CS    .    .    .    .    .    .  
 1 |  .    .    .    .    .    .    .    .    .    .  
   + --------------------------------------------------
       1    2    3    4    5    6    7    8    9   10
```

| Label | Repo | Read |
|---|---|---|
| `SB` | `skillbox` | Thin and strongly agent-oriented |
| `DBX` | `jetify-com/devbox` | Thin, environment-focused, less agent-native |
| `OVS` | `gitpod-io/openvscode-server` | Remote IDE wrapper |
| `CS` | `coder/code-server` | Browser IDE product, very popular, less agent-native |
| `DP` | `loft-sh/devpod` | Thinner remote workflow tool |
| `E2B` | `e2b-dev/E2B` | Agent runtime, more sandbox-oriented than workstation-oriented |
| `CDR` | `coder/coder` | Heavier remote-dev platform with some agent fit |
| `GP` | `gitpod-io/gitpod` | Full remote-dev platform |
| `DT` | `daytonaio/daytona` | Heavier and highly agent/runtime-oriented |

## Evidence From Comparable Repos

The issue signals below were pulled with `gh` on April 1, 2026.

### 1. Heavy platforms validate demand, but also show control-plane drag

- [coder/coder#23889](https://github.com/coder/coder/issues/23889): long-lived `coder ssh` sessions behind identity-aware proxies
- [coder/coder#23900](https://github.com/coder/coder/issues/23900): AI prompt field bug
- [gitpod-io/gitpod#18109](https://github.com/gitpod-io/gitpod/issues/18109): local SSH config friction for VS Code Desktop
- [daytonaio/daytona#4232](https://github.com/daytonaio/daytona/issues/4232): self-hosted runners failing to connect to the API
- [daytonaio/daytona#4230](https://github.com/daytonaio/daytona/issues/4230): session commands hanging on heredocs and long-running subprocesses

Takeaway: the market is real, but the platform path is expensive.

### 2. Thin tools validate the desire for simpler private workflows, but leave gaps

- [loft-sh/devpod#1967](https://github.com/loft-sh/devpod/issues/1967): dotfile changes require delete and recreate
- [loft-sh/devpod#1965](https://github.com/loft-sh/devpod/issues/1965): Cursor extension install path assumptions break
- [jetify-com/devbox#2797](https://github.com/jetify-com/devbox/issues/2797): preload Devbox environments for LLM agents and CI
- [coder/code-server#4212](https://github.com/coder/code-server/issues/4212): store state on remote instead of browser
- [gitpod-io/openvscode-server#653](https://github.com/gitpod-io/openvscode-server/issues/653): maintenance and security freshness concerns

Takeaway: users want simpler private setups, but still need durable state and
agent-oriented ergonomics.

### 3. Agent runtimes validate adjacent demand, but at a different layer

- [e2b-dev/E2B#1238](https://github.com/e2b-dev/E2B/issues/1238): template filesystem changes not persisted into sandboxes
- [e2b-dev/E2B#1074](https://github.com/e2b-dev/E2B/issues/1074): reconnect to a running command fails after two minutes
- [e2b-dev/E2B#1160](https://github.com/e2b-dev/E2B/issues/1160): credential and secret brokering for outbound requests
- [e2b-dev/E2B#1138](https://github.com/e2b-dev/E2B/issues/1138): skills integration for AI agents

Takeaway: strong evidence for agent runtime demand, but not the same thing as a
durable personal workstation.

## Strategic Non-Goals

`skillbox` should not become:

- a multi-tenant workspace control plane
- a browser IDE product
- a generic sandbox provider
- a replacement for app-specific deployment systems or CI
- a hosted SaaS with hidden infrastructure

## Product Test

New work should pass a simple filter:

- Does this make one private box more durable, legible, or agent-friendly?
- Or does it drag the project toward platform sprawl?

If the answer looks more like Coder, Gitpod, Daytona, or E2B than
`skillbox`, the burden of proof should be high.
