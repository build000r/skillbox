<div align="center">

# skillbox

**A thin, self-hosted Tailnet monoserver for AI-assisted coding, with client-scoped overlays.**

![runtime](https://img.shields.io/badge/runtime-Docker-2496ED?style=flat-square&logo=docker&logoColor=white)
![access](https://img.shields.io/badge/access-Tailscale-242424?style=flat-square&logo=tailscale&logoColor=white)
![shape](https://img.shields.io/badge/shape-thin%20starter-6E7781?style=flat-square)
![doctor](https://img.shields.io/badge/doctor-manifest%20checks-2ea44f?style=flat-square)

</div>

```bash
cp .env.example .env
make doctor
make runtime-sync
make dev-sanity
make build
make up
make shell
```

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
- mount `home/.claude` and `home/.codex` into that box
- mount the host parent directory at `/monoserver` for client repo roots
- optionally run a workspace-local `swimmers` API against the same tmux namespace as the agents
- keep one stable core machine and layer client-specific overlays on top
- declare the inside of the box with a runtime graph for repos, artifacts, installed skills, services, logs, and checks
- declare one-shot bootstrap tasks and let services pull them in automatically
- start and stop declared service graphs in dependency order with one command
- focus on a client workspace with live state collection, enriched agent context, and continuous drift monitoring
- provision and tear down remote boxes from the operator machine via MCP tools
- pin and package default skills locally
- validate outer drift with `make doctor` and inner drift with `make dev-sanity`

### Why Use `skillbox`?

| Need | `skillbox` answer |
|---|---|
| Private access without public SSH exposure | Tailscale host access plus host hardening scripts |
| A workspace that feels like a narrowed local setup | One bind-mounted `/workspace`, plus `/monoserver` for sibling repo roots and client overlays |
| A sane way to let the box grow over time | `workspace/runtime.yaml` plus `.env-manager/manage.py` manage the core machine plus client-specific repos, artifacts, installed skills, logs, and checks |
| Service graphs that do not devolve into shell folklore | Declared `depends_on` edges let `up`, `down`, and `restart` expand and order service graphs automatically |
| Live drift detection and auto-healing | The pulse daemon monitors services on a fixed interval, auto-restarts crashes, and emits structured events to a JSONL journal |
| One-command client activation | `focus` syncs, bootstraps, starts services, collects live state, and writes enriched agent context in a single pass |
| Fleet management from the operator machine | The operator MCP server provisions DO droplets, enrolls Tailscale, and runs commands on remote boxes as native agent tools |
| Reproducible default skills | `03-skill-sync.sh` packages from a pinned manifest and vendored local packager |
| Confidence that docs/config/runtime still match | `04-reconcile.py` powers `make render` and `make doctor`, while `make dev-sanity` validates the box internals |
| Minimal surface area | No multi-tenant control plane, no hosted dependency, no hidden sibling repo requirement for packaging |

## Quick Example

This is the shortest useful local run:

```bash
cp .env.example .env
make render
make doctor
make runtime-render
make runtime-sync
make dev-sanity
make runtime-status CLIENT=personal
make build
make up
make up-surfaces
./scripts/03-skill-sync.sh
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/v1/sandbox
curl -fsS http://127.0.0.1:8000/v1/runtime
make shell
```

What that gives you:

- a validated box model
- a validated runtime graph for the inside of the box
- a running workspace container
- a mounted `/monoserver` view of the host parent directory
- optional API and web inspection surfaces
- packaged default `.skill` bundles under `default-skills/`
- installed default skills under `home/.claude/skills` and `home/.codex/skills`
- generated agent context at `home/.claude/CLAUDE.md` with a symlink at `home/.codex/AGENTS.md`

## Focus

The `focus` command is the single-command path from "I want to work on this
client" to "everything is running and the agents know about it":

```bash
python3 .env-manager/manage.py focus personal --format json
```

What this does in one pass:

1. **Sync** — creates managed directories, installs skills, renders config
2. **Bootstrap** — runs declared one-shot tasks in dependency order
3. **Up** — starts services with healthcheck waits
4. **Collect** — snapshots live state: git branches, service health, recent log errors
5. **Context** — writes enriched `CLAUDE.md` with live status tables, attention items, and recent activity
6. **Persist** — saves `.focus.json` so `--resume` can re-activate later

The enriched context adds live sections that the static `context` command does not:

- a **Live Status** table showing service state, PID, and health
- a **Repo State** table showing branch, dirty file count, and last commit
- a **Recent Activity** feed from the event journal
- an **Attention** section highlighting failing checks, downed services, and recent log errors

Resume the last session without re-running the full pipeline:

```bash
python3 .env-manager/manage.py focus --resume
```

## Pulse Daemon

The pulse daemon watches the runtime graph on a fixed interval and reacts to
drift:

```bash
make pulse-start        # start the daemon (foreground, logs to logs/runtime/pulse.log)
make pulse-status       # print cycle count, heals, service states
make pulse-stop         # send SIGTERM
```

What it does each cycle:

- reloads `runtime.yaml` and detects config hash changes
- probes every declared service and detects state transitions
- auto-restarts crashed managed services with exponential backoff
- runs declared checks and detects failures and recoveries
- writes every state change to the JSONL event journal at `logs/runtime/journal.jsonl`
- persists a state snapshot at `logs/runtime/pulse.state.json` for the MCP tool to read

The journal is queryable via the `skillbox_journal` MCP tool inside the
container or via `query_journal()` in Python.

## Swimmers Overlay

If tmux-backed agents live inside the `workspace` container, the clean path is
to run `swimmers` there too and keep the TUI on the operator machine.

```bash
make swimmers-install
make swimmers-start
make swimmers-status
make swimmers-runtime-status
```

What this overlay does:

- starts the `workspace` container with `docker-compose.swimmers.yml`
- keeps the API inside the same container and tmux namespace as the agents
- installs a runnable binary at `/home/sandbox/.local/bin/swimmers`
- can hydrate that binary from `SKILLBOX_SWIMMERS_DOWNLOAD_URL` without a sibling repo checkout
- still supports source-building from the optional `/monoserver/swimmers` checkout when you have it
- records process state and logs under `logs/swimmers/`

Safety model:

- the swimmers port is `3210`
- the compose overlay publishes only to `127.0.0.1` by default
- remote access requires opting in with `SKILLBOX_SWIMMERS_PUBLISH_HOST=0.0.0.0`
- non-loopback publishing is blocked unless `SKILLBOX_SWIMMERS_AUTH_MODE=token` and `SKILLBOX_SWIMMERS_AUTH_TOKEN` are set

Remote operator example:

```bash
# on the client skillbox
cat >> .env <<'EOF'
SKILLBOX_SWIMMERS_PUBLISH_HOST=0.0.0.0
SKILLBOX_SWIMMERS_AUTH_MODE=token
SKILLBOX_SWIMMERS_AUTH_TOKEN=replace-me
EOF
make swimmers-start

# on the operator machine
AUTH_MODE=token AUTH_TOKEN=replace-me \
SWIMMERS_TUI_URL=http://<tailnet-ip>:3210 \
cargo run --bin swimmers-tui
```

## Agent Context

The runtime graph describes the inside of the box. The `context` command makes
that description available to the agents running inside it.

```bash
make context CLIENT=personal
```

What this does:

- reads the resolved runtime graph for the active client and profiles
- generates `home/.claude/CLAUDE.md` with repos, services, tasks, skills,
  logs, and runnable make commands
- creates a symlink at `home/.codex/AGENTS.md` pointing to the same file
- re-runs automatically on `make runtime-sync`

When an agent starts a session inside the workspace container, it reads its
home `CLAUDE.md` or `AGENTS.md` and immediately knows:

- which client context it is operating in
- which repos are available and where they live
- which services exist and how to start, stop, or tail them
- which bootstrap tasks are available and how to run them
- which skills are installed
- where logs go
- the exact make commands for health checks, status, and sync

This means the agent does not need to be told any of this manually. Every repo,
service, task, or skill you add to `runtime.yaml` or a client overlay
automatically appears in the agent context the next time `sync` or `context`
runs.

Both files are gitignored because they are generated state that varies by
environment and client selection.

## Fleet Management

The operator MCP server (`scripts/operator_mcp_server.py`) exposes box
lifecycle as native agent tools, so Claude Code on the operator machine can
provision, inspect, and tear down remote boxes without leaving the
conversation.

```bash
# provision a new box (dry-run first, always)
# via MCP: operator_provision { box_id: "acme-prod", profile: "dev-large", dry_run: true }

# or via make targets
make box-up BOX=acme-prod PROFILE=dev-large
make box-status BOX=acme-prod
make box-list
make box-ssh BOX=acme-prod
make box-down BOX=acme-prod
```

Available MCP tools:

| Tool | Purpose |
|---|---|
| `operator_boxes` | List all active boxes from inventory |
| `operator_profiles` | List available box profiles (region, size, image) |
| `operator_box_status` | Deep health probe for a specific box |
| `operator_provision` | Full zero-to-running provision flow |
| `operator_teardown` | Full teardown: drain, remove from Tailnet, destroy droplet |
| `operator_box_exec` | Run a command on a remote box over Tailscale SSH |
| `operator_compose_up` | Build and start local containers |
| `operator_compose_down` | Stop all local containers |
| `operator_doctor` | Run outer validation checks |
| `operator_render` | Print the resolved sandbox model |

### Destructive Operation Guard

Destructive tools (`operator_teardown`, `operator_compose_down`) are gated by
a PreToolUse hook (`scripts/guard-destructive-op.sh`) that blocks execution
unless:

1. `dry_run=true` was passed (preview mode always passes)
2. All git repos in the workspace are committed and pushed
3. A dry-run was already executed this session

This prevents accidental infrastructure destruction with uncommitted work.

## Design Philosophy

### 1. Thin beats magical

This repo intentionally stops well short of Coder or Daytona. It focuses on one operator-controlled box and a small set of explicit scripts.

### 2. Host SSH, container work

SSH lands on the host. Docker Compose runs the workspace and optional surfaces. The container is where your day-to-day work should feel familiar.

### 3. Declarative enough to check, not so abstract it disappears

`workspace/sandbox.yaml`, `workspace/dependencies.yaml`, `workspace/runtime.yaml`, and the skill manifests describe the intended box. `make doctor` checks the outer shell, and `make dev-sanity` checks the interior graph plus managed artifact and skill install state.

### 4. Portable skill packaging matters

The default skill packaging chain is vendored locally. A fresh clone does not need a sibling `../opensource` checkout just to package default skills.

### 5. Local-first operator ergonomics

The repo includes enough surfaces to inspect and validate the shape, but not so much that it becomes a platform you have to operate before you can work.

### 6. The box should describe its internals, not just its container

The new internal `.env-manager` layer is intentionally small. It does not try to become a second platform; it gives the box one declared source of truth for repos, artifacts, installed skills, services, logs, and sanity checks so the workspace can accrete without turning into guesswork.

### 7. Continuous observation, not just point-in-time checks

The pulse daemon and event journal give the box a memory. Instead of only
checking state when you ask, the box continuously monitors itself and records
what happened, so agents and operators can query recent history rather than
re-deriving it from scratch.

## Comparison

| Option | Best for | Tradeoff |
|---|---|---|
| `skillbox` | One private dev box with curated repos and skills | You operate the host and Docker yourself |
| Raw droplet + ad hoc shell setup | Fastest one-off experiments | Hard to reproduce, harder to hand off |
| Coder | Multi-user remote dev environments | Heavier platform and control-plane overhead |
| Daytona | Managed workspace orchestration | More moving parts than a single private box needs |

## Installation

There is no packaged release or curl installer yet. Today, installation means cloning or copying the repo, then choosing the workflow that matches where you are.

### Option 1: Local checkout

```bash
cp .env.example .env
make doctor
make runtime-sync
make dev-sanity
make build
make up
make shell
```

### Option 2: Existing Linux host or droplet

```bash
cp .env.example .env
cd scripts
sudo ./01-bootstrap-do.sh
sudo TAILSCALE_AUTHKEY="tskey-..." TAILSCALE_HOSTNAME="skillbox-dev" ./02-install-tailscale.sh
cd ..
make doctor
make runtime-sync
make dev-sanity
make build
make up
```

### Option 3: Operator-provisioned remote box

If the operator MCP server is configured, provision a box from Claude Code:

```bash
# MCP: operator_provision { box_id: "dev-01", profile: "dev-small", dry_run: true }
# Review dry-run output, confirm, then:
# MCP: operator_provision { box_id: "dev-01", profile: "dev-small" }
```

Or via make:

```bash
make box-up BOX=dev-01 PROFILE=dev-small
```

### Option 4: Copy into an existing repo workspace

If you already have a private server or local repo root and just want the shape:

```bash
cp -R skillbox /path/to/your/workspace/skillbox
cd /path/to/your/workspace/skillbox
cp .env.example .env
make doctor
make runtime-sync
```

## Quick Start

1. Copy the environment template.

   ```bash
   cp .env.example .env
   ```

2. Review the resolved box model.

   ```bash
   make render
   ```

3. Run the drift and readiness checks.

   ```bash
   make doctor
   make runtime-sync
   make dev-sanity
   ```

4. Build and start the workspace.

   ```bash
   make build
   make up
   ```

5. Optionally start the inspection surfaces.

   ```bash
   make up-surfaces
   ```

6. Enter the workspace shell.

   ```bash
   make shell
   ```

## Command Reference

### Make targets

| Command | What it does |
|---|---|
| `make bootstrap-env` | Copies `.env.example` to `.env` if needed |
| `make render` | Prints the resolved sandbox model |
| `make doctor` | Validates manifest/runtime drift, Compose wiring, and skill sync |
| `make runtime-render` | Prints the resolved internal runtime graph |
| `make runtime-sync` | Creates managed repo/log directories and installs declared skills with generated lockfiles for the active core/client scope |
| `make runtime-status` | Summarizes declared repos, skills, tasks, services, logs, and checks |
| `make runtime-bootstrap` | Syncs runtime state and runs declared bootstrap tasks for the active scope |
| `make runtime-up` | Syncs runtime state, runs required bootstrap tasks, and starts manageable services for the active scope |
| `make runtime-down` | Stops manageable services for the active scope |
| `make runtime-restart` | Restarts manageable services for the active scope |
| `make runtime-logs` | Shows recent service logs for the active scope |
| `make onboard` | Scaffold and activate a new client overlay with optional blueprint |
| `make context` | Generates `CLAUDE.md` and `AGENTS.md` from the resolved runtime graph |
| `make dev-sanity` | Validates the internal runtime graph, filesystem readiness, and managed skill integrity |
| `make pulse-start` | Starts the pulse reconciliation daemon |
| `make pulse-stop` | Sends SIGTERM to the running pulse daemon |
| `make pulse-status` | Prints pulse daemon status: cycles, heals, service states |
| `make build` | Builds the workspace image |
| `make up` | Starts the workspace container |
| `make up-surfaces` | Starts the API and web stub surfaces |
| `make down` | Stops all containers |
| `make shell` | Opens a shell inside the workspace container |
| `make logs` | Tails compose logs |
| `make swimmers-install` | Installs a runnable swimmers binary inside the workspace container |
| `make swimmers-start` | Starts swimmers inside the workspace container with the swimmers compose overlay |
| `make swimmers-stop` | Stops the managed swimmers process |
| `make swimmers-restart` | Restarts the managed swimmers process |
| `make swimmers-status` | Reports swimmers process and probe state inside the workspace container |
| `make swimmers-logs` | Tails swimmers server logs from inside the workspace container |
| `make swimmers-runtime-status` | Shows the runtime-manager view of the swimmers overlay |
| `make box-up` | Provision a new remote box (DO + Tailscale) |
| `make box-down` | Tear down a remote box |
| `make box-status` | Health-check a remote box |
| `make box-list` | List all boxes from inventory |
| `make box-ssh` | SSH into a remote box |
| `make box-profiles` | List available box profiles |

### Scripts

| Script | Purpose | Example |
|---|---|---|
| `scripts/01-bootstrap-do.sh` | Bootstrap a fresh Ubuntu or DigitalOcean host | `sudo ./scripts/01-bootstrap-do.sh` |
| `scripts/02-install-tailscale.sh` | Join the tailnet and harden SSH | `sudo TAILSCALE_AUTHKEY="tskey-..." ./scripts/02-install-tailscale.sh` |
| `scripts/03-skill-sync.sh` | Resolve, stage, validate, and package default skills | `./scripts/03-skill-sync.sh --dry-run` |
| `scripts/04-reconcile.py render` | Print the resolved sandbox model | `python3 scripts/04-reconcile.py render --with-compose` |
| `scripts/04-reconcile.py doctor` | Run drift and readiness checks | `python3 scripts/04-reconcile.py doctor` |
| `scripts/05-swimmers.sh` | Manage the workspace-local swimmers install and process lifecycle | `./scripts/05-swimmers.sh status` |
| `scripts/operator_mcp_server.py` | Operator MCP server for fleet and container lifecycle | Runs via `.mcp.json` as `skillbox-operator` |
| `scripts/guard-destructive-op.sh` | PreToolUse hook gating destructive operator tools | Called automatically by Claude Code hooks |
| `.env-manager/manage.py context` | Generate CLAUDE.md and AGENTS.md from the resolved runtime graph | `python3 .env-manager/manage.py context --client personal` |
| `.env-manager/manage.py focus` | Activate a client with live state and enriched context | `python3 .env-manager/manage.py focus personal --format json` |
| `.env-manager/manage.py render` | Print the resolved internal runtime graph | `python3 .env-manager/manage.py render --format json` |
| `.env-manager/manage.py sync` | Create managed repo/artifact/log directories and install declared skills for the selected core/client scope | `python3 .env-manager/manage.py sync --client personal --dry-run` |
| `.env-manager/manage.py doctor` | Validate the internal repos/skills/logs/check graph for the selected core/client scope | `python3 .env-manager/manage.py doctor --client personal` |
| `.env-manager/manage.py status` | Summarize repo, artifact, skill, task, service, log, and health state for the selected core/client scope | `python3 .env-manager/manage.py status --client personal` |
| `.env-manager/manage.py bootstrap` | Sync runtime state and run declared bootstrap tasks in dependency order | `python3 .env-manager/manage.py bootstrap --client acme-studio --task app-bootstrap` |
| `.env-manager/manage.py up` | Sync runtime state, run any service-declared bootstrap tasks, and start manageable services, expanding declared `depends_on` prerequisites and waiting for healthchecks when present | `python3 .env-manager/manage.py up --profile surfaces --service api-stub` |
| `.env-manager/manage.py down` | Stop manageable services started by the runtime manager, stopping selected dependents before their prerequisites | `python3 .env-manager/manage.py down --profile surfaces --service api-stub` |
| `.env-manager/manage.py restart` | Restart manageable services for the selected core/client scope, preserving declared dependency order | `python3 .env-manager/manage.py restart --profile surfaces --service web-stub` |
| `.env-manager/manage.py logs` | Print recent log output for declared services | `python3 .env-manager/manage.py logs --profile surfaces --service api-stub --lines 80` |
| `.env-manager/manage.py client-init` | Scaffold a new client overlay, optionally applying a reusable blueprint for repos and services | `python3 .env-manager/manage.py client-init acme-studio --blueprint git-repo --set PRIMARY_REPO_URL=https://github.com/acme/app.git` |
| `.env-manager/pulse.py` | Pulse reconciliation daemon for continuous drift detection and auto-heal | `python3 .env-manager/pulse.py run --interval 30` |

## Configuration

### Environment defaults

`.env.example` sets the main runtime paths, ports, and optional integrations:

```dotenv
SKILLBOX_NAME=skillbox
SKILLBOX_WORKSPACE_ROOT=/workspace
SKILLBOX_REPOS_ROOT=/workspace/repos
SKILLBOX_SKILLS_ROOT=/workspace/skills
SKILLBOX_LOG_ROOT=/workspace/logs
SKILLBOX_HOME_ROOT=/home/sandbox
SKILLBOX_MONOSERVER_ROOT=/monoserver
SKILLBOX_MONOSERVER_HOST_ROOT=..
SKILLBOX_API_PORT=8000
SKILLBOX_WEB_PORT=3000
SKILLBOX_SWIMMERS_PORT=3210
SKILLBOX_SWIMMERS_PUBLISH_HOST=127.0.0.1
SKILLBOX_SWIMMERS_REPO=/monoserver/swimmers
SKILLBOX_SWIMMERS_INSTALL_DIR=/home/sandbox/.local/bin
SKILLBOX_SWIMMERS_BIN=/home/sandbox/.local/bin/swimmers
SKILLBOX_SWIMMERS_DOWNLOAD_URL=
SKILLBOX_SWIMMERS_AUTH_MODE=
SKILLBOX_SWIMMERS_AUTH_TOKEN=
SKILLBOX_SWIMMERS_OBSERVER_TOKEN=
SKILLBOX_DCG_BIN=/home/sandbox/.local/bin/dcg
SKILLBOX_DCG_DOWNLOAD_URL=
SKILLBOX_DCG_PACKS=core.git,core.filesystem
SKILLBOX_PULSE_INTERVAL=30
SKILLBOX_DCG_MCP_PORT=3220
SKILLBOX_FWC_BIN=/home/sandbox/.local/bin/fwc
SKILLBOX_FWC_DOWNLOAD_URL=
SKILLBOX_FWC_MCP_PORT=3221
SKILLBOX_FWC_ZONE=work
SKILLBOX_FWC_CONNECTORS=github,slack,linear
SKILLBOX_DO_TOKEN=
SKILLBOX_DO_SSH_KEY_ID=
SKILLBOX_TS_AUTHKEY=
```

`SKILLBOX_SWIMMERS_REPO` is now an optional source checkout path. If you set
`SKILLBOX_SWIMMERS_DOWNLOAD_URL`, `make runtime-sync` or `make swimmers-install`
can hydrate the binary without needing `/monoserver/swimmers`.

`SKILLBOX_DO_TOKEN`, `SKILLBOX_DO_SSH_KEY_ID`, and `SKILLBOX_TS_AUTHKEY` are
required only for fleet management (`operator_provision` / `make box-up`).

### Default skill sources

`workspace/default-skills.sources.yaml` pins where bundled skills come from:

```yaml
version: 1

sources:
  - kind: github
    repo: https://github.com/build000r/skills
    sha: 9f0f69029aee5c6b247bf25dcfe13e34f2110e3a

  - kind: local
    path: ./skills
```

### Default skill manifest

`workspace/default-skills.manifest` is just the list of skill names to preload:

```text
ask-cascade
```

### Generated skill lockfiles

`make runtime-sync` writes `workspace/default-skills.lock.json` for the shared
default skill set, and selected client overlays write their own lockfiles under
`workspace/clients/<client>/skills.lock.json`.

Each lockfile records:

- the current manifest and sources-config digests
- the bundle digests for each declared default skill
- the installed tree hashes for the managed Claude and Codex skill homes

These lockfiles are generated state and are gitignored, so running sync does
not turn normal local runtime reconciliation into noisy repo dirt.

### Sandbox model

`workspace/sandbox.yaml` declares the box shape:

```yaml
version: 1

sandbox:
  name: skillbox
  purpose: cloneable-tailnet-dev-box
  runtime:
    mode: tailnet-docker
    agent_user: sandbox
    tailnet_enabled: true
    ssh_enabled: true
    ssh_mode: host
  ports:
    api: 8000
    web: 3000
  paths:
    workspace_root: /workspace
    repos_root: /workspace/repos
    skills_root: /workspace/skills
    log_root: /workspace/logs
    claude_root: /home/sandbox/.claude
    codex_root: /home/sandbox/.codex
    monoserver_root: /monoserver
```

### Runtime graph

`workspace/runtime.yaml` declares the core inside of the box:

```yaml
version: 2

selection: {}

core:
  repos:
    - id: skillbox-self
      path: ${SKILLBOX_WORKSPACE_ROOT}
    - id: managed-repos
      path: ${SKILLBOX_REPOS_ROOT}
  skills:
    - id: default-skills
      manifest: ${SKILLBOX_WORKSPACE_ROOT}/workspace/default-skills.manifest
  tasks:
    - id: app-bootstrap
      repo: skillbox-self
      command: ./scripts/bootstrap-app.sh
      success:
        type: path_exists
        path: ${SKILLBOX_LOG_ROOT}/runtime/app-bootstrap.ok
  services:
    - id: internal-env-manager
    - id: api-stub
      profiles: [surfaces]
    - id: web-stub
      profiles: [surfaces]
  checks:
    - id: monoserver-root
      path: ${SKILLBOX_MONOSERVER_ROOT}
```

`depends_on` is optional. For example:

```yaml
services:
  - id: api
  - id: web
    depends_on: [api]
```

When it is present, `up --service <id>` pulls in the full prerequisite chain
first, while `down --service <id>` and `restart --service <id>` stop
dependents before prerequisites and then bring the graph back in topological
order.

Tasks are the one-shot companion to services. A task declares a command,
optional task-to-task `depends_on`, a success check, and optional `inputs` and
`outputs`. `bootstrap --task <id>` runs the selected task graph in dependency
order, and `up` automatically runs any tasks named under a service's
`bootstrap_tasks` list before trying to launch the service.

Client overlays are auto-discovered from `workspace/clients/<client>/overlay.yaml`.
For example:

```yaml
version: 1

client:
  id: personal
  default_cwd: ${SKILLBOX_MONOSERVER_ROOT}
  repo_roots:
    - id: personal-root
      path: ${SKILLBOX_MONOSERVER_ROOT}
  skills:
    - id: personal-skills
      manifest: ${SKILLBOX_WORKSPACE_ROOT}/workspace/clients/personal/skills.manifest
```

Create a new overlay scaffold with:

```bash
python3 .env-manager/manage.py client-init acme-studio
```

Or start from a built-in client blueprint that wires repos, services, logs, and
checks into the scaffold:

```bash
python3 .env-manager/manage.py client-init --list-blueprints
python3 .env-manager/manage.py client-init acme-studio \
  --blueprint git-repo \
  --set PRIMARY_REPO_URL=https://github.com/acme/app.git
python3 .env-manager/manage.py client-init acme-studio \
  --blueprint git-repo-http-service \
  --set PRIMARY_REPO_URL=https://github.com/acme/app.git \
  --set SERVICE_COMMAND='pnpm dev'
python3 .env-manager/manage.py client-init acme-studio \
  --blueprint git-repo-http-service-bootstrap \
  --set PRIMARY_REPO_URL=https://github.com/acme/app.git \
  --set BOOTSTRAP_COMMAND='pnpm install && pnpm prisma migrate deploy && mkdir -p .skillbox && touch .skillbox/bootstrap.ok' \
  --set SERVICE_COMMAND='pnpm dev'
```

Blueprints keep the default client skills scaffold but append client-scoped
repos, artifacts, tasks, services, logs, and checks to the generated overlay,
so the next `render`, `sync`, `bootstrap`, `status`, `doctor`, or `up` command
already has something concrete to operate on.

### Managed Env Files

Client overlays and client blueprints can now declare `env_files` alongside
repos, artifacts, services, logs, and checks. This is the missing bridge
between "the repo cloned" and "the repo is runnable on a fresh droplet".

Example:

```yaml
client:
  env_files:
    - id: app-env
      kind: dotenv
      repo: app
      path: ${PRIMARY_REPO_PATH}/.env.local
      required: true
      profiles:
        - core
      source:
        kind: file
        path: ./workspace/secrets/clients/${CLIENT_ID}/app.env
      sync:
        mode: write
```

What this does:

- `make runtime-sync CLIENT=acme-studio` writes the declared env file into the target repo with `0600` permissions
- `make dev-sanity CLIENT=acme-studio` fails if a required env source is missing
- `make runtime-status CLIENT=acme-studio` reports whether the env file is present, stale, or missing
- `make runtime-up CLIENT=acme-studio SERVICE=app-dev` refuses to launch the service until required env files are ready

### Managed Bootstrap Tasks

Client overlays and blueprints can now declare `tasks` alongside repos, env
files, services, logs, and checks.

Example:

```yaml
client:
  tasks:
    - id: app-bootstrap
      kind: bootstrap
      repo: app
      command: pnpm install && pnpm prisma migrate deploy && touch .skillbox/bootstrap.ok
      outputs:
        - ${PRIMARY_REPO_PATH}/.skillbox/bootstrap.ok
      success:
        type: path_exists
        path: ${PRIMARY_REPO_PATH}/.skillbox/bootstrap.ok
  services:
    - id: app-dev
      bootstrap_tasks:
        - app-bootstrap
```

What this does:

- `make runtime-bootstrap CLIENT=acme-studio TASK=app-bootstrap` runs the selected task graph in dependency order
- `make runtime-status CLIENT=acme-studio` reports each task as `ready`, `pending`, or `blocked`
- `make dev-sanity CLIENT=acme-studio` warns when declared bootstrap outputs are still missing
- `make runtime-up CLIENT=acme-studio SERVICE=app-dev` automatically runs `app-bootstrap` before starting the service

### Client Selection

The mental model is now:

- `core` is the monoserver itself
- `--client` selects a client overlay
- `--profile` selects optional non-client overlays such as `surfaces`
- projects live inside a client overlay, not the other way around

Examples:

```bash
python3 .env-manager/manage.py render --client personal
python3 .env-manager/manage.py sync --client personal
python3 .env-manager/manage.py status --client vibe-coding-client
python3 .env-manager/manage.py doctor --client vibe-coding-client
python3 .env-manager/manage.py client-init acme-studio
python3 .env-manager/manage.py client-init acme-studio --blueprint git-repo --set PRIMARY_REPO_URL=https://github.com/acme/app.git
python3 .env-manager/manage.py render --client personal --profile surfaces

make runtime-sync CLIENT=personal
make runtime-status CLIENT=vibe-coding-client
make dev-sanity CLIENT=vibe-coding-client
make runtime-render CLIENT=personal PROFILE=surfaces
```

That makes `skillbox` behave less like one static starter and more like a
personal environment compiler: one monoserver can describe multiple client
overlays without forcing every repo, service, log, skill set, and check to
exist all the time.

## Architecture

```text
                Tailscale SSH
                      │
                      ▼
            ┌──────────────────────┐
            │   Host machine       │
            │  Ubuntu / Docker     │
            ├──────────────────────┤
            │ scripts/01,02        │
            │ docker-compose.yml   │
            └──────────┬───────────┘
                       │
          ┌────────────┴────────────┐
          │                         │
          ▼                         ▼
┌───────────────────┐      ┌───────────────────┐
│ workspace         │      │ optional surfaces │
├───────────────────┤      ├───────────────────┤
│ /workspace        │      │ api :8000         │
│ /monoserver       │      │ web :3000         │
│ /workspace/repos  │      └───────────────────┘
│ /workspace/skills │
│ /workspace/logs   │
│ /home/.claude     │
│ /home/.codex      │
└─────────┬─────────┘
          │
          ▼
┌─────────────────────────────────────────┐
│ declarative control layers              │
├─────────────────────────────────────────┤
│ workspace/sandbox.yaml                  │
│ workspace/dependencies.yaml             │
│ workspace/runtime.yaml                  │
│ 04-reconcile.py                         │
│ .env-manager/manage.py                  │
│ .env-manager/pulse.py                   │
│ 03-skill-sync.sh / package_skill.py     │
└───────────────────┬─────────────────────┘
                    │
                    ▼
       ┌──────────────────────────────────┐
       │ managed box internals            │
       ├──────────────────────────────────┤
       │ repos, artifacts, skills, checks │
       │ api/web stub health probes       │
       │ default skill bundles + lockfiles│
       │ event journal (journal.jsonl)    │
       │ pulse state (pulse.state.json)   │
       └──────────────────────────────────┘

┌─────────────────────────────────────────┐
│ operator machine (outside the box)      │
├─────────────────────────────────────────┤
│ scripts/operator_mcp_server.py          │
│   → operator_provision                  │
│   → operator_teardown                   │
│   → operator_box_exec                   │
│   → operator_compose_up/down            │
│ scripts/guard-destructive-op.sh         │
│ scripts/box.py (DO + Tailscale fleet)   │
│ workspace/boxes.json (inventory)        │
└─────────────────────────────────────────┘
```

## MCP Integration

Skillbox exposes two MCP servers for different contexts:

### Inside the box (agent tools)

`.env-manager/mcp_server.py` runs inside the workspace container and gives
agents tools to manage their own environment:

| Tool | Purpose |
|---|---|
| `skillbox_status` | Runtime status for repos, services, tasks, checks |
| `skillbox_render` | Resolved runtime graph |
| `skillbox_sync` | Sync state (create dirs, install skills) |
| `skillbox_up` / `skillbox_down` | Start/stop services |
| `skillbox_logs` | Recent service log output |
| `skillbox_bootstrap` | Run declared bootstrap tasks |
| `skillbox_focus` | Activate a client with live state and enriched context |
| `skillbox_onboard` | Scaffold and bootstrap a new client |
| `skillbox_client_init` | Create a new client overlay from blueprint |
| `skillbox_pulse` | Query pulse daemon status |
| `skillbox_journal` | Query the event journal |

### Outside the box (operator tools)

`scripts/operator_mcp_server.py` runs on the operator machine and provides
fleet lifecycle tools. See the [Fleet Management](#fleet-management) section.

## Troubleshooting

### `make doctor` fails on Compose validation

Check that Docker is installed and `docker compose config --format json` works on the host.

### `make dev-sanity` warns about missing log directories or managed skill installs

That is expected on a fresh clone. The core runtime graph declares
`logs/runtime` and `logs/repos`, and the managed skill install roots plus
lockfile are also created on demand.

Run:

```bash
make runtime-sync
make dev-sanity
```

### `make up-surfaces` starts but ports are unreachable

The API and web surfaces bind to `127.0.0.1` by design. Use local forwarding or a host shell, not a public interface.

### `03-skill-sync.sh` fails during packaging

Run:

```bash
./scripts/03-skill-sync.sh --dry-run
python3 scripts/package_skill.py /path/to/a/skill ./dist
```

If the validator fails, fix the skill frontmatter or filtered file contents first.

### SSH works to the host but not the box

That is expected. SSH targets the host, not the workspace container. Use `make shell` after connecting.

### `make runtime-status` shows repos as missing

The internal runtime manager evaluates host paths that correspond to the
container's `/workspace/...` and `/monoserver/...` trees.

Run:

```bash
make runtime-sync
make runtime-status
```

If a repo is still missing after sync, the runtime entry is probably configured
with `sync.mode: external` and expects a bind mount from `/monoserver` or a
manual clone under `/workspace/repos`.

If the missing repo belongs to a client overlay, check it explicitly:

```bash
make runtime-status CLIENT=personal
make runtime-status CLIENT=vibe-coding-client
```

### Default skills look stale

Re-run:

```bash
./scripts/03-skill-sync.sh
make runtime-sync
make doctor
```

### Pulse daemon won't start

Check if it's already running:

```bash
make pulse-status
```

If the PID file is stale (process died without cleanup), remove it:

```bash
rm logs/runtime/pulse.pid
make pulse-start
```

### Operator tools are blocked by the guard hook

The destructive-op guard requires:
1. All git repos committed and pushed
2. A `dry_run=true` call before the real operation

Run `/commit`, push, then re-run with `dry_run: true` first.

## Limitations

- This is not a hosted control plane or a multi-user workspace platform.
- There is no release installer, package manager distribution, or cloud provisioning flow yet beyond the operator MCP tools.
- The API and web surfaces are inspection stubs, not a full UI.
- The internal runtime manager now does dependency-aware task and service orchestration plus managed env hydration, but it still does not try to replace app-specific deployment systems or CI.
- Secrets management and app-specific bootstrap details beyond what you declare in your overlays and blueprints are still your responsibility.
- The pulse daemon is single-process; it does not survive container restarts unless declared as a managed service in `runtime.yaml`.
- Fleet management requires DigitalOcean and Tailscale credentials. Other cloud providers are not supported.
- There is no license file in this repo yet. Add one before publishing it as open source.

## FAQ

### Is SSH supposed to go to the host or the container?

The host. The container is the workspace runtime, not the SSH target.

### Why keep `home/.claude` and `home/.codex` in the repo?

So the box shape stays reproducible. You can replace the placeholder contents with your own baseline configs.

### Why is there both `make doctor` and `make render`?

`make render` shows the intended model. `make doctor` checks whether the intended model and the runnable repo state still agree.

### Why is there both `workspace/dependencies.yaml` and `workspace/runtime.yaml`?

`workspace/dependencies.yaml` describes the runtime categories the box exposes. `workspace/runtime.yaml` declares the interior graph the new internal manager actually operates on: repos, artifacts, installed skills, services, logs, and checks.

### Why ship a vendored skill packager?

So default skill packaging works from this repo alone. You do not need a sibling checkout just to build bundled `.skill` files.

### Should `default-skills/*.skill` live in the repo?

In this starter, yes. They represent the packaged default bundles the box can ship with. Other ad hoc `.skill` outputs should stay local.

### Can I add real repos under `repos/`?

Yes, but think of that as starter behavior. The longer-term model is one
monoserver plus one or more client overlays, each with its own repo roots,
skills, services, logs, and checks.

### What is `/monoserver` for?

It is the host parent directory mounted into the workspace container. That is
how client overlays can point at sibling roots such as the broader personal
repo universe or a client directory like `../vibe-coding-client`.

### Is `.env-manager/` the same thing as `../.env-manager`?

No. The outer `../.env-manager` launches boxes from outside. The in-repo `.env-manager/` manages the inside of this box.

### What is the difference between `focus` and `context`?

`context` generates a static CLAUDE.md from the declared runtime graph.
`focus` does everything `context` does but also syncs, bootstraps, starts
services, collects live state, and writes an enriched context with real-time
service health, git state, recent errors, and journal activity.

### What is the event journal?

An append-only JSONL file at `logs/runtime/journal.jsonl`. Every significant
runtime event (service start/stop/crash, sync completion, focus activation,
pulse restarts) is recorded with a timestamp, type, subject, and detail
object. Agents can query it via the `skillbox_journal` MCP tool.

### How does the destructive-op guard work?

It is a bash script (`scripts/guard-destructive-op.sh`) registered as a
Claude Code PreToolUse hook. It intercepts `operator_teardown` and
`operator_compose_down` calls and blocks them unless all repos are clean and
pushed, and a dry-run was already executed this session.

## About Contributions

> *About Contributions:* Please don't take this the wrong way, but I do not accept outside contributions for any of my projects. I simply don't have the mental bandwidth to review anything, and it's my name on the thing, so I'm responsible for any problems it causes; thus, the risk-reward is highly asymmetric from my perspective. I'd also have to worry about other "stakeholders," which seems unwise for tools I mostly make for myself for free. Feel free to submit issues, and even PRs if you want to illustrate a proposed fix, but know I won't merge them directly. Instead, I'll have Claude or Codex review submissions via `gh` and independently decide whether and how to address them. Bug reports in particular are welcome. Sorry if this offends, but I want to avoid wasted time and hurt feelings. I understand this isn't in sync with the prevailing open-source ethos that seeks community contributions, but it's the only way I can move at this velocity and keep my sanity.

## License

No license file is included yet.
