# Skillbox → Real Dev → Prod Roadmap

How skillbox transitions from agent/skill development runtime to a
production-grade devbox layer that promotes cleanly into your existing prod
stack (whatever its shape — nginx + compose on a droplet, k8s, Fly, Render,
etc.).

> The phases below are written abstractly. They name the *roles* a piece of
> infrastructure plays ("your prod reverse proxy", "your prod env file
> layout") rather than any concrete service. Operators wire those roles to
> their own private stack inside their `skillbox-config` overlay.

---

## Phase 0: Already Here (Now)

Skillbox as a dev runtime is production-grade. The runtime graph, client
overlays, service lifecycle, health checks, installer, pulse daemon — all of
this works today and is in use for real development: building skills, running
agents, managing clients.

The private skill distribution loop is also present: `skill-repos.yaml` can mix
repo, path, and distributor-backed skill sources; local publishing creates
signed schema v2 manifests with per-version artifacts; preview resolves pins
and floors without mutating state; explicit sync installs the selected artifact
and records distributor state; rollback can reinstall a verified cached bundle.
The worker-runtime broker contract is present at the CLI/MCP boundary: callers
can submit open-ended tasks, poll run status, read terminal artifacts, and
explicitly promote reviewed learning proposals without coupling skillbox to a
particular chat harness. The launch boundary is explicit now: resolved runs
write `task.json`, invoke a configured Hermes command, and return
`WORKER_LAUNCH_FAILED` when no runtime is installed or launch exits non-zero.
The repo now includes `scripts/hermes_codex_adapter.py` as one concrete
adapter: operators can set `SKILLBOX_WORKER_HERMES_COMMAND` to that script and
run Codex-backed workers through the same broker contract. Production use still
requires the operator to install/authenticate the selected runtime and pin the
command in the active environment.
The current local quality bar is explicit: `make python-cov-xml` passes with
877 tests, 1 skipped, and 85% total line coverage; full `.env-manager` CRAP is 22.20;
the Codex adapter CRAP is 9.00; and a real Codex-backed broker run succeeded as
`wr_20260505_073758_d41cda`.
The hosted distributor service, standalone laptop UX, short-lived token
exchange, and background update checks remain later distribution work.

What's missing isn't the box — it's the **bridge to whatever nginx/deploy
layer your prod stack already has** so the box can serve as the staging /
devbox tier of a real deploy pipeline.

---

## Phase 1.5: Shared Jam — Trusted Collaborator Access

**Goal:** The operator can invite trusted collaborators to jam on the same
box — same repos, same services, same containers — with attribution at the
shell and git layer.

### What it does

- **Tailnet membership = access list.** Invite/revoke via `scripts/03-shared-jam.sh`. No secondary auth layer.
- **Identity from Tailscale.** On SSH login, `tailscale whois` resolves the peer's identity. Git author, tmux session name, and shared history are all attributed automatically.
- **Shared everything.** Everyone SSHs as the same Linux user (`sandbox`). Same repos, same Docker socket, same `.claude/`, same services. No isolation, by design.

### Scripts

| Script | Purpose |
|--------|---------|
| `scripts/03-shared-jam.sh` | `invite`, `revoke`, `list`, `status` subcommands |
| `scripts/skillbox-login.sh` | ForceCommand login hook (deployed to `/usr/local/bin/`) |

### Gate

`sudo ./scripts/03-shared-jam.sh invite alice@example.com` shares the box,
and Alice SSHs in with her identity auto-resolved and attributed.

See [docs/shared-jam.md](shared-jam.md) for the full usage guide.

---

## Phase 1: Skillbox-as-Devbox

**Goal:** Skillbox becomes the canonical place where you develop *and
preview* services before they hit your prod stack.

### What needs to happen

- **Wire the reverse proxy stub.** The `docker-compose.yml` ships with `api`
  and `web` stub services. Replace those stubs with actual service
  containers (or proxy-pass to local processes) so you get
  `localhost:3001/3002` preview URLs inside the box.
- **Client overlay for your prod app.** Declare a real client overlay that
  mounts your app's dev containers as skillbox services with health checks
  in `runtime.yaml`. The overlay is the seam between the public engine and
  your private app shape.
- **Shared network bridge.** If your prod compose uses dedicated Docker
  networks for the reverse proxy and inter-service calls, declare a parallel
  `skillbox-dev` network that the dev containers can join, so box services
  can talk to the rest of your stack locally.

### Gate

A `skillbox render && skillbox up` brings up your dev stack alongside the
core box, with health checks passing.

---

## Phase 2: Dev → Staging Parity

**Goal:** What runs in the box matches what runs in prod, minus the
domain/SSL.

First compiler slice is present: `python3 .env-manager/manage.py parity-report
<client> --format json` reads the selected runtime graph plus the client's
overlay `production_stack` contract and reports reverse-proxy, env,
healthcheck, deploy-mode, network, and runtime parity-ledger rows as `ready`,
`missing`, `drift`, `deferred`, or `not_assessed`. The command is read-only and
is summarized inside `stewardship-report`; it does not yet generate or apply
nginx/compose/deploy files.

- **Mirror your prod reverse-proxy config locally.** Take your prod
  `nginx.conf` (or equivalent) and create a `skillbox-dev` variant — same
  upstream blocks, same rate-limit zones, but pointing at local containers
  instead of prod. This catches routing bugs before they hit the host.
- **Env parity.** Whatever env layering your prod stack uses
  (`local.env` / `override.env` / `prod.env`, dotenv stacks, secret
  managers, etc.), add a `skillbox.env` profile that the runtime manager
  renders from `runtime.yaml` declarations so you get the same env var
  shape in both environments.
- **Deploy skill integration.** The `deploy` skill already knows about
  modes, hosts, and health endpoints. Add a `skillbox-local` mode that
  targets the box's containers instead of SSH'ing to your prod host. Same
  skill, different target.

### Gate

Local reverse-proxy routing matches the prod shape. Env vars render
identically between box and prod host.

---

## Phase 3: Clean Prod Transition

**Goal:** The `deploy` skill promotes from skillbox-dev to your prod stack
with one command.

- **GHCR (or equivalent) image pipeline.** If your prod stack already pulls
  images from a registry, skillbox services that are ready for prod get the
  same treatment — Dockerfile → registry → `deploy.sh` pulls on the host.
- **Reverse-proxy site templating.** New services developed in skillbox get
  their own `.conf` (or k8s manifest, or Fly app, etc.) generated from the
  runtime graph — same pattern as your existing apps, just a new upstream
  block.
- **Blue-green via the box.** If your prod stack has blue/green or rolling
  deploy support, the devbox becomes the "blue" environment for smoke
  testing before the prod host's "green" gets swapped.
- **Tailscale as the glue.** Box and prod host are on the same tailnet.
  Promotion is just "build here, push image, pull there."

### Gate

One deploy command promotes a service from the box to the prod host with no
manual reverse-proxy or compose editing.

---

## Architecture

```
┌─────────────────────────────────────────────┐
│  Skillbox (your machine / DO devbox)        │
│  ┌─────────────┐  ┌──────────────────────┐  │
│  │ runtime.yaml│→ │ dev containers       │  │
│  │ + overlays  │  │ (your app's services)│  │
│  └─────────────┘  └──────────┬───────────┘  │
│                              │ tailnet      │
│  ┌───────────────────────────▼───────────┐  │
│  │ skillbox-dev nginx (mirrors prod)     │  │
│  └───────────────────────────────────────┘  │
└──────────────────────┬──────────────────────┘
                       │ promote (registry image push)
┌──────────────────────▼──────────────────────┐
│  Your prod host                             │
│  ┌───────────────────────────────────────┐  │
│  │ reverse proxy (SSL, rate-limit)       │  │
│  │ → your services                       │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

---

## Timeline

| Phase | Milestone | Gate |
|-------|-----------|------|
| **0 (Now)** | Skillbox works for skill/agent dev | Already passing |
| **1.5** | Trusted collaborator access via Tailnet | `03-shared-jam.sh invite` + SSH with auto-attribution |
| **1** | Your dev containers declared in runtime.yaml | `skillbox up` runs your app locally |
| **2** | Reverse-proxy parity + env parity | Local routing matches prod shape |
| **3** | Deploy skill promotes box → prod host | One command, same skill, different target |

---

## Key Insight

No new infra needs to be built. Whatever your prod stack already does for
nginx + deploy, skillbox layers next to it through a client overlay and a
shared Docker network. Everything after that is parity and polish.
