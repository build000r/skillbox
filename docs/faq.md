# FAQ

This page contains content moved from `README.md` during the README front-door split.

## TL;DR

### The Problem

Most remote dev setups overshoot the need. You want one private box that feels
like a real computer: one primary workspace container, your own Claude or
Codex home directories, a broader repo universe, and a way to activate the
right client context without standing up a full hosted workspace control plane.

### The Solution

`skillbox` gives you a cloneable starter for a Tailnet-first dev box:

- SSH to the host over Tailscale
- run one main Docker workspace container
- persist agent homes, logs, client overlays, and optional repo roots under `SKILLBOX_STATE_ROOT` (`./.skillbox-state` by default)
- mount that durable state into the box at `/home/sandbox`, `/workspace/logs`, `/workspace/workspace/clients`, and `/monoserver`
- optionally run a workspace-local `swimmers` API against the same tmux namespace as the agents
- keep one stable core machine and layer client-specific overlays on top
- declare the inside of the box with a runtime graph for repos, artifacts, installed skills, services, logs, and checks
- declare one-shot bootstrap tasks and let services pull them in automatically
- start and stop declared service graphs in dependency order with one command
- focus on a client workspace with live state collection, enriched agent context, and continuous drift monitoring
- record runtime activity in `runtime.log` and durable client-scoped session timelines
- broker open-ended worker runs through stable CLI/MCP submit, status, artifact, and learning-promotion surfaces
- provision and tear down remote boxes from the operator machine via MCP tools
- declare skill repos and sync skills from GitHub repos or local paths
- validate outer drift with `make doctor` and inner drift with `make dev-sanity`

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

Most comparable tools land in one of three buckets:

- heavy remote-dev platforms with control-plane overhead
- thin environment tools that still leave durable workspace state to you
- agent sandboxes optimized for secure ephemeral execution rather than a durable personal box

`skillbox` is deliberately aimed at the narrower gap between those buckets: one
private machine that feels like a real computer for one operator and their
agents, with persistent homes, repo overlays, explicit runtime state, and low
operational ceremony.

For the deeper thesis, see [docs/VISION.md](VISION.md): mission, vision,
values, competitive fit, non-goals, and the market map that explains why
`skillbox` intentionally stops short of Coder, Gitpod, Daytona, and E2B.
## Design Stance

### 1. Single-tenant first

`skillbox` is for one operator-controlled machine, not a multi-tenant workspace
platform. Trusted collaborator access is still valid, but it is an explicit
shared-box mode on top of a single machine, not a separate tenancy model.

### 2. Durable state beats ephemeral sandboxes

The box should remember your repos, agent homes, overlays, logs, and recent
runtime history. The goal is a machine that carries context forward, not an
execution substrate you keep rebuilding from scratch.

### 3. Host SSH, container work

SSH lands on the host. Docker Compose runs the workspace and optional surfaces.
The container is where your day-to-day work should feel familiar.

### 4. Explicit runtime graphs beat hidden control planes

`workspace/sandbox.yaml`, `workspace/dependencies.yaml`,
`workspace/runtime.yaml`, and the skill repo configs describe the intended box.
`make doctor` checks the outer shell, and `make dev-sanity` checks the interior
graph plus managed artifact and skill install state.

### 5. Skill content is local and lockfile-backed

Skills are declared in `workspace/skill-repos.yaml` and materialized by
explicit sync. GitHub repos and local paths stay the default development path:
`sync` clones or references them, filtered-copies skill directories into agent
homes, and records resolved SHAs in a lock file. Distributor bundles are allowed
only as signed sync-time inputs; they are verified, unpacked into local files,
and recorded in the same lockfile rather than fetched live at agent runtime.

### 6. Local-first operator ergonomics

The repo includes enough surfaces to inspect and validate the shape, but not so
much that it becomes a platform you have to operate before you can work.

### 7. The box should describe its internals, not just its container

The internal `.env-manager` layer is intentionally small. It does not try to
become a second platform; it gives the box one declared source of truth for
repos, artifacts, installed skills, services, logs, and sanity checks so the
workspace can accrete without turning into guesswork.

### 8. Continuous observation beats point-in-time checks

The pulse daemon, runtime log, and durable session timeline give the box a memory. Instead of only
checking state when you ask, the box continuously monitors itself and records
what happened, so agents and operators can query recent history rather than
re-deriving it from scratch.
## Comparison

| Option | Best for | What it gets right | Why `skillbox` still exists |
|---|---|---|---|
| `skillbox` | One private agent-friendly box with durable repos, homes, and overlays | Low ceremony, explicit runtime state, Tailscale-first access | You still operate the host and Docker yourself |
| Raw droplet + ad hoc shell setup | One-off experiments | Fastest path to "something works" | Hard to reproduce, drift-prone, weak handoff story |
| Devbox / DevPod | Reproducible environments or thin BYO-remote workflows | Better environment packaging and remote IDE integration | Not the same thing as one durable private box with agent context and client overlays |
| Coder / Gitpod | Multi-user remote dev platforms | Stronger policy, team workflows, browser and IDE integrations | More platform and control-plane overhead than a single operator usually needs |
| Daytona / E2B | Secure agent runtimes and sandbox orchestration | Better isolation and ephemeral execution controls | Solves a different layer than "my durable private machine" |

**Use `skillbox` when:**

- you want one private machine for you and your agents
- durable state matters more than sandbox churn
- SSH and Tailscale are a feature, not a compromise
- you want explicit runtime declarations instead of a hosted control plane
## FAQ

### Popular Related Tools & Apps

These come up often because they sit near `skillbox`, but they do not all solve
the same layer of the problem. For the deeper positioning thesis, see
[docs/VISION.md](VISION.md).

### Is `skillbox` an alternative to OpenClaw?

Not really. OpenClaw is closer to a personal-agent or agent-platform product.
`skillbox` is the private machine shape underneath: one durable box with your
repos, homes, overlays, logs, and runtime state. They are adjacent, and can
coexist, but they are not clean substitutes.

### Is `skillbox` like Claude Cowork?

No. Claude Cowork is an app-layer agent experience for knowledge work. It lives
much closer to the desktop product surface. `skillbox` lives lower in the
stack: host access, workspace container, durable repos, agent homes, and box
operations. Cowork answers "what should the agent do for me?"; `skillbox`
answers "where does that durable work live?"

### Is `skillbox` competing with Coder or Gitpod?

Only indirectly. Coder and Gitpod are remote-dev platforms with much more
control-plane, policy, and multi-user surface area. `skillbox` is for the case
where you want one private operator-owned box and do not want to stand up a
full platform.

### Is `skillbox` competing with Daytona or E2B?

They solve an adjacent layer. Daytona and E2B are much more about agent
runtimes, sandbox orchestration, isolation, and secure execution. `skillbox` is
about a durable private workstation for you and your agents, not an ephemeral
sandbox substrate.

### Is `skillbox` the same thing as Devbox or DevPod?

No. Devbox and DevPod are closer to environment tooling or thinner remote-dev
flows. `skillbox` goes one level up and models the whole box: access, homes,
repos, overlays, services, runtime checks, event history, and agent context.

### When should I use `skillbox` instead of those tools?

Use `skillbox` when the job is "give me one private machine that feels like my
computer, but works well for agents too." If the job is browser IDEs, multi-user
workspace fleets, or untrusted-code sandboxing, a different tool is usually the
better fit.

### Is SSH supposed to go to the host or the container?

The host. The container is the workspace runtime, not the SSH target.

### Why keep agent homes under `.skillbox-state/home/`?

So the checkout stays source-only while agent homes survive rebuilds and
container restarts. Inside the box those same directories mount at
`/home/sandbox/.claude` and `/home/sandbox/.codex`.

### Why is there both `make doctor` and `make render`?

`make render` shows the intended model. `make doctor` checks the outer repo shell for drift against that model: manifests, Compose wiring, and the default `skill-repo-set` sync path. `make dev-sanity` checks the live runtime state after sync/bootstrap.

### Why is there both `workspace/dependencies.yaml` and `workspace/runtime.yaml`?

`workspace/dependencies.yaml` describes the runtime categories the box exposes. `workspace/runtime.yaml` declares the interior graph the new internal manager actually operates on: repos, artifacts, installed skills, services, logs, and checks.

### How are skills installed?

`workspace/skill-repos.yaml` declares GitHub repos and local paths. `sync`
clones repos into `workspace/skill-repos/`, filtered-copies skill directories
(respecting `.skillignore`) into `/home/sandbox/.claude/skills/` and `/home/sandbox/.codex/skills/`
inside the container, backed by `.skillbox-state/home/.claude/skills/` and
`.skillbox-state/home/.codex/skills/` on the host,
and writes a lock file with resolved commit SHAs. Client overlays can declare
their own `skill-repos.yaml` for client-specific skills.

### Can I add real repos under `repos/`?

Yes, but think of that as starter behavior. The longer-term model is one
monoserver plus one or more client overlays, each with its own repo roots,
skills, services, logs, and checks.

### What is `/monoserver` for?

It is the runtime path for the persistent monoserver tree. By default the host
side lives at `${SKILLBOX_STATE_ROOT}/monoserver` and mounts into the workspace
container at `/monoserver`. If you prefer a sibling checkout layout, override
`SKILLBOX_MONOSERVER_HOST_ROOT`.

### Is `.env-manager/` the same thing as `../.env-manager`?

No. The outer `../.env-manager` launches boxes from outside. The in-repo `.env-manager/` manages the inside of this box.

### What is the difference between `focus` and `context`?

`context` generates a static CLAUDE.md from the declared runtime graph.
`focus` does everything `context` does but also syncs, bootstraps, starts
services, collects live state, and writes an enriched context with real-time
service health, git state, recent errors, and runtime activity.

### What is the runtime log?

A plain-text log at `.skillbox-state/logs/runtime/runtime.log` on the host,
mounted at `/workspace/logs/runtime/runtime.log` inside the container. Pulse,
sync, focus, and other runtime paths append human-readable status lines there
so recent activity can be surfaced without another public activity service.

### How should agents store durable notes or memory?

Use the `skillbox_session_*` MCP tools for client-scoped session timelines and
`cm` for longer-lived procedural memory. `focus` reads recent runtime activity
from `runtime.log`; no separate public sidecar flow is required.

### How does the destructive-op guard work?

It is a bash script (`scripts/guard-destructive-op.sh`) registered as a
Claude Code PreToolUse hook. It intercepts `operator_teardown` and
`operator_compose_down` calls and blocks them unless all repos are clean and
pushed, and a dry-run was already executed this session. It also matches
`operator_box_exec` as a backup screen (catastrophic-pattern block only) — the
real `operator_box_exec` command gate lives server-side in
`scripts/operator_mcp_server.py` so it protects every MCP client. See
[operator_box_exec command gate](operations.md#operator_box_exec-command-gate).
