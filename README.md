<div align="center">

# skillbox

**A private, single-tenant Tailnet box for you and your coding agents.**

Thin, self-hosted, Docker-based, with durable runtime state under `.skillbox-state/` and client-scoped overlays.

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
| Private access without public SSH exposure | Tailscale host access plus host hardening scripts |
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

For the deeper thesis, see [docs/VISION.md](docs/VISION.md): mission, vision,
values, competitive fit, non-goals, and the market map that explains why
`skillbox` intentionally stops short of Coder, Gitpod, Daytona, and E2B.

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
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/v1/sandbox
curl -fsS http://127.0.0.1:8000/v1/runtime
make shell
```

What that gives you:

- a validated box model
- a validated runtime graph for the inside of the box
- a running workspace container
- a durable runtime state root at `${SKILLBOX_STATE_ROOT:-./.skillbox-state}`
- a mounted `/monoserver` view backed by `${SKILLBOX_STATE_ROOT:-./.skillbox-state}/monoserver`
- optional API and web inspection surfaces
- cloned skill repos under `workspace/skill-repos/`
- installed default skills under `.skillbox-state/home/.claude/skills/` and `.skillbox-state/home/.codex/skills/`
- generated agent context at `home/.claude/CLAUDE.md` with a symlink at `home/.codex/AGENTS.md`

> **About the `personal` client.** Throughout this README, `personal` is an
> example client slug. Clients are operator-owned private overlays — they are
> **not** part of a fresh clone, so a default checkout reports `active_clients: []`
> and any `--client personal` / `CLIENT=personal` command fails with an explicit
> "no client overlays are attached" recovery message until you attach one. Create
> your own with `python3 .env-manager/manage.py client-init <id>` (or the
> `onboard <id>` / `make first-box CLIENT=<id>` macros), then substitute that slug
> for `personal` below. Core-scoped commands (`render`, `doctor`, `dev-sanity`,
> `up`) need no client.

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
- a **Runtime Activity** feed from the runtime log and active durable sessions
- an **Attention** section highlighting failing checks, downed services, and recent log errors

Resume the last session without re-running the full pipeline:

```bash
python3 .env-manager/manage.py focus --resume
```

Use `stewardship-report` when you need a shareable operator evidence packet
instead of another raw status dump:

```bash
python3 .env-manager/manage.py stewardship-report personal --format md --write
```

The report is read-only unless `--write` or `--output-dir` is passed. It
combines the current runtime graph, live checks, service probes, recent log
errors, `.focus.json`, pulse state, durable sessions, and parity-ledger coverage
into a risk-first packet. It also marks important hardening domains that are not
yet proven by the public runtime graph, such as backup/restore drills and cost
review, as `not_assessed`.

Use `parity-report` before claiming a client is shaped like production:

```bash
python3 .env-manager/manage.py parity-report personal --format json
```

The report is read-only. It compares the selected client's runtime graph
against the client's `production_stack` contract in its overlay, covering
reverse-proxy routes, env-file expectations, health endpoints, deploy modes,
optional network assumptions, and the existing runtime parity ledger. Rows are
classified as `ready`, `missing`, `drift`, `deferred`, or `not_assessed`; JSON
output includes the source declaration, expected contract, actual runtime
evidence, and next safe action for every non-ready row. `stewardship-report`
includes a compact dev/prod parity evidence summary.

## Skillbox Forge

Skillbox Forge is the operator loop for turning real agent-session signal into
reviewed skill updates. It is passive by default: hooks collect evidence, status
summarizes the signal store, and proposals stay on `forge/<skill>` branches
until you explicitly accept or reject them.

Bootstrap the scoring hooks once:

```bash
python3 .env-manager/manage.py forge init --format json
# optional fallback for missed session hooks
python3 .env-manager/manage.py forge init --with-cron --format json
```

`forge init` is idempotent. It adds a Claude Code `SessionEnd` hook, patches the
installed `codex-tmux` wrapper when present, and can add a cron fallback. The
hook target is `scripts/score-session.sh`; it exits 0 when `skill-issue` is not
installed or no new transcripts are available, so session teardown is not
blocked by scoring.

After normal Claude/Codex work accumulates signal in
`~/.claude/skill-review-history.jsonl`, inspect it:

```bash
python3 .env-manager/manage.py forge status
python3 .env-manager/manage.py forge status --skill ask-cascade --format json
```

When a skill has enough scored sessions, create a reviewable proposal:

```bash
python3 .env-manager/manage.py forge propose ask-cascade --dry-run
python3 .env-manager/manage.py forge propose ask-cascade --min-sessions 5
```

`forge propose` requires a clean skill repo, sufficient signal, and no existing
`forge/<skill>` branch. It writes a minimal `SKILL.md` mutation on that branch
and logs the proposal to `~/.claude/forge-proposals.jsonl`. Review the branch
diff in the skill repo before deciding:

```bash
git -C workspace/skill-repos/<repo> diff main..forge/ask-cascade
python3 .env-manager/manage.py forge accept ask-cascade
# or
python3 .env-manager/manage.py forge reject ask-cascade --reason "too broad"
```

Accept fast-forward merges the proposal, deletes the forge branch, and logs to
`~/.claude/forge-decisions.jsonl`. Reject deletes the branch and records the
reason. After accepting, run the normal skill installation path so agent homes
pick up the reviewed skill change:

```bash
make runtime-sync
make dev-sanity
```

`make doctor` surfaces Forge trust checks without mutating state:
`SKILL_FORGE_HOOK_MISSING` when hooks are absent, `SKILL_FORGE_STALE` when
declining scored signal has no proposal, `SKILL_FORGE_PENDING` for open
`forge/*` branches, and `SKILL_FORGE_UNSCORED` when recent transcripts have not
been scored. Dirty repos, existing proposal branches, missing skills, and
fast-forward failures are intentional stop conditions; inspect and resolve the
skill repo before rerunning the Forge command.

## Local Runtime Profiles

Client overlays declare **local runtime profiles** — namespaced service groups
(`local-*`) that own the daily local development loop directly, without
shelling out to legacy bash orchestration.

### Canonical daily path

A `local-core` profile in your client overlay declares the daily local
development loop — the set of services you want `up` together, in dependency
order, with health waits. The runtime owns env hydration, bootstrap, start
order, health waits, status, logs, and teardown for every service in the
profile.

```bash
# Hydrate env, run bridge, write enriched context for local-core
python3 .env-manager/manage.py focus <client> --profile local-core --format json

# Start the full core loop in dependency order, in reuse mode
python3 .env-manager/manage.py up --client <client> --profile local-core --mode reuse

# Same, but use each repo's prod-backed local path where it differs
python3 .env-manager/manage.py up --client <client> --profile local-core --mode prod

# Same, but use each repo's reset/fresh-restore path
python3 .env-manager/manage.py up --client <client> --profile local-core --mode fresh

# Inspect state for the active profile
python3 .env-manager/manage.py status --client <client> --profile local-core

# Tail one service
python3 .env-manager/manage.py logs --client <client> --service <service-id>
```

### Declaring covered services

`local-core` covers whatever services your overlay declares for it. Repo
roots are resolved under `${SKILLBOX_MONOSERVER_ROOT}` and each repo
provides its own start command; the overlay declares dependency order, env
targets, and health probes.

The cut-over contract that backs this path is the six-service legacy core
loop:

- `spaps`: repo root `sweet-potato`; depends on none; health
  `http://localhost:3301/health`; modes `reuse`, `prod`, `fresh`.
- `htma_server`: repo root `htma_server`; depends on `spaps`; health
  `http://localhost:8000/health`; modes `reuse`, `prod`, `fresh`.
- `ingredient_server`: repo root `ingredient_server`; depends on `spaps`;
  health `http://localhost:8001/health`; modes `reuse`, `prod`, `fresh`.
- `approval_feedback_api`: repo root
  `unclawg/services/approval_feedback_api`; depends on `spaps`; health
  `http://localhost:8010/health`; modes `reuse`, `prod`, `fresh`.
- `cfo`: repo root `cfo`; depends on `spaps`; health `localhost:8050`;
  modes `reuse`, `prod`, `fresh`.
- `htma`: repo root `htma`; depends on `spaps` and `htma_server`; health
  `http://localhost:5173`; modes `reuse`, `prod`, `fresh`.

Do not infer coverage for adjacent local surfaces such as `buildooor`,
`cca-website`, `unclawg`, `voice-to-text`, `swimmers`, or `videos` from this
table. Their current lifecycle behavior comes from the selected client's
`parity_ledger`; only rows marked `covered` are managed by the normal runtime
lifecycle.

Covered services may declare any subset of `--mode reuse`, `--mode prod`, and
`--mode fresh`. The runtime validates the selected mode against each service
before starting anything and returns `LOCAL_RUNTIME_MODE_UNSUPPORTED` if a
requested service cannot honor it.

### Start modes

| Mode | Meaning |
|------|---------|
| `reuse` | Reuse healthy local dependencies and existing local DB state where the repo supports it |
| `prod` | Use the repo's prod-backed local path when that path differs from the default |
| `fresh` | Use the repo's reset or fresh-restore path when the repo supports it |

`--mode` replaces the legacy `db=reuse|prod|fresh` flag from `project.sh up`.

### Env bridge

Covered services consume generated env files. The overlay declares a
`local-core-bridge` task that wraps the legacy `../../.env-manager/sync.sh`
compiler as an explicit, temporary seam. `focus` checks bridge freshness
(output mtime vs overlay mtime) and re-runs only when stale. Direct lifecycle
requests against `sync.sh` return `LOCAL_RUNTIME_SERVICE_DEFERRED`; failures
inside a covered workflow surface as `LOCAL_RUNTIME_ENV_BRIDGE_FAILED`.

`sync.sh` is a bridge seam, not a supported surface. A later slice will
replace it with native env layering.

Bridge-related error codes:

| Code | When |
|------|------|
| `LOCAL_RUNTIME_ENV_BRIDGE_FAILED` | Bridge task exits non-zero |
| `LOCAL_RUNTIME_ENV_OUTPUT_MISSING` | Generated env file absent after bridge |

### local-minimal subsets

Overlays can declare strict subsets of `local-core` (for example a
`local-minimal` profile that covers only the frontend slice plus its server
dependency). A subset shares the same overlay declarations, the same
`--mode` contract, and runs off its own bridge (which compiles only the env
targets the subset needs). Use a subset when you do not need the full daily
loop running.

```bash
python3 .env-manager/manage.py up --client <client> --profile local-minimal --mode reuse
```

### Profile namespace

Local profiles use the `local-*` prefix to avoid collisions with box-level
profiles (`core`, `surfaces`, `connectors`). Selecting any `local-*` profile
also activates `core` automatically.

Typical profiles an overlay might declare:

| Profile | Coverage |
|---------|----------|
| `local-core` | The full daily loop your overlay declares as covered |
| `local-minimal` | A strict subset of `local-core` for a narrower slice |
| `local-backend` | Backend subset of `local-core` |
| `local-frontend` | Frontend subset of `local-core` |
| `local-all` | Union of all covered local-runtime services |

The runtime resolves each profile by filtering the same covered-service set;
it does not introduce new services outside `local-core`. Any legacy target
that is not yet native is recorded in the parity ledger (see below) and
rejected at request time with `LOCAL_RUNTIME_SERVICE_DEFERRED`.

Local web services that should be reachable from a Tailnet browser use stable
per-app ports bound to the Tailnet address. See
[Tailnet App Addressing](docs/tailnet-ingress.md) for the browser URL contract,
app config checklist, and deferred ingress alternatives.

### Parity ledger

Everything from the old `.env-manager` bash surface that `local-core` does not
cover natively is declared in the overlay's `parity_ledger` section. The
runtime enforces that ledger directly: `up`, `status`, `logs`, and `doctor`
all read it, and any request for a non-covered surface fails with a stable
error code instead of a silent no-op.

Each entry records:

- `legacy_surface` — the original `sync.sh` / `project.sh` name
- `surface_type` — `service`, `env_target`, `helper`, or `flag`
- `action` — `declare`, `bridge`, `build`, or `drop`
- `ownership_state` — `covered`, `bridge-only`, `deferred`, or `external`
- `intended_profiles` — which future `local-*` profile should own it
- `bridge_dependency` — which bridge, if any, the surface still runs through
- `request_error` — the error code returned for direct lifecycle requests

Each overlay records its own ownership states. The shape looks like:

| State | What it means |
|-------|---------------|
| `covered` | Surfaces fully owned by the runtime, including supported `--mode` values |
| `bridge-only` | Surfaces still running through a temporary seam (e.g. `sync.sh` env compilation) |
| `deferred` | Surfaces acknowledged but not yet covered — requesting them returns `LOCAL_RUNTIME_SERVICE_DEFERRED` |
| `external` | Surfaces explicitly owned outside this overlay |

Deferred surfaces are acknowledged, not supported. Requesting one through the
runtime returns `LOCAL_RUNTIME_SERVICE_DEFERRED`; adding one requires a
follow-on slice that moves it from `deferred` to `covered`. Drift between the
declared ledger and the covered set surfaces as `LOCAL_RUNTIME_COVERAGE_GAP`
in `status` and `doctor`.

### Error codes

| Code | When |
|------|------|
| `LOCAL_RUNTIME_PROFILE_UNKNOWN` | Requested profile has no declared services |
| `LOCAL_RUNTIME_START_BLOCKED` | Dependency or readiness blocks launch |
| `LOCAL_RUNTIME_SERVICE_DEFERRED` | Requested surface is in the parity ledger but not covered |
| `LOCAL_RUNTIME_MODE_UNSUPPORTED` | Start mode not declared for a requested service |
| `LOCAL_RUNTIME_COVERAGE_GAP` | Declared ledger and covered set have drifted |

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
- writes every state change to the plain-text runtime log at `logs/runtime/runtime.log`
- persists a state snapshot at `logs/runtime/pulse.state.json` for the MCP tool to read

For durable, work-specific notes, use the `skillbox_session_*` MCP tools for
client-scoped session timelines and `cm` for procedural memory. `focus`
surfaces recent runtime activity directly from `runtime.log`.

## Worker Runtime Broker

Skillbox can accept open-ended worker tasks without becoming the chat harness.
The broker records the task, selected runtime, state, artifacts, and learning
proposals under the state root, while callers interact through a stable contract:

```bash
python3 .env-manager/manage.py worker-submit analysis "Inspect this repo" --client skills --cwd "$PWD" --format json
python3 .env-manager/manage.py worker-status wr_YYYYMMDD_HHMMSS_abcdef --format json
python3 .env-manager/manage.py worker-artifacts wr_YYYYMMDD_HHMMSS_abcdef --format json
python3 .env-manager/manage.py worker-promote-learning lp_001 --target-kind skill --target-location opensource/skills/report-analyst --format json
```

The first runtime id is `hermes`, kept behind the broker contract. A resolved
run writes `task.json`, then launches the first configured command from
`SKILLBOX_WORKER_HERMES_COMMAND`, `SKILLBOX_HERMES_COMMAND`,
`SKILLBOX_WORKER_HERMES_BIN`, `SKILLBOX_HERMES_BIN`, or `hermes` on `PATH`.
The command receives `SKILLBOX_ROOT_DIR`, `SKILLBOX_WORKER_RUN_ID`,
`SKILLBOX_WORKER_TASK_PATH`, and `SKILLBOX_WORKER_RESULT_PATH`; it should write a
JSON result with `state`, `summary`, optional `artifacts`, and optional
`learning_proposals`. If no Hermes command is installed or the command exits
non-zero, the run ends as `failed` with `WORKER_LAUNCH_FAILED`.

This repo ships a Codex-backed adapter for environments where the operator has
the Codex CLI installed:

```bash
export SKILLBOX_WORKER_HERMES_COMMAND="python3 scripts/hermes_codex_adapter.py"
python3 .env-manager/manage.py worker-submit analysis "Inspect this repo" --client skills --cwd "$PWD" --format json
```

The adapter runs `codex exec` read-only and writes the broker result file. Set
`SKILLBOX_HERMES_CODEX_BIN` when `codex` is not on `PATH`, and
`SKILLBOX_HERMES_CODEX_MODEL` to pin a model for dogfood or production runs.

Pending learning proposals are never written back automatically; they must be
promoted explicitly after review.

Use a real active client id from the resolved runtime graph. In this checkout,
`make dev-sanity CLIENT=skills` is valid, and `CLIENT=skillbox` is valid when
the attached `SKILLBOX_CLIENTS_HOST_ROOT` contains the Skillbox overlay. The
broker can record submit/status/artifact/promotion state today and has an
explicit launch boundary; successful execution still depends on a configured
Hermes command for the active environment.

Current local proof for this broker/runtime surface:

- `make python-cov-xml` passes with 877 tests, 1 skipped, and 85% total line coverage.
- `python3 ../skills/crap/scripts/analyze_crap.py .env-manager --languages python --threshold 30 --top 20` passes with `FINAL_SCORE: 22.20`.
- `python3 ../skills/crap/scripts/analyze_crap.py scripts/hermes_codex_adapter.py --languages python --threshold 20 --top 20` passes with `FINAL_SCORE: 9.00`.
- A real Codex-backed broker run succeeded as `wr_20260505_073758_d41cda` using `SKILLBOX_WORKER_HERMES_COMMAND='python3 scripts/hermes_codex_adapter.py'`.

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
- remote access requires opting in with `SKILLBOX_SWIMMERS_PUBLISH_HOST=0.0.0.0` (the helper exports this as `SWIMMERS_BIND`)
- remote box helpers only promote loopback defaults to public bind when `SKILLBOX_SWIMMERS_EXPOSE=1` is set
- non-loopback publishing is blocked unless `SKILLBOX_SWIMMERS_AUTH_MODE=token` and `SKILLBOX_SWIMMERS_AUTH_TOKEN` are set
- remote box status prints the canonical phone/browser URL as `Open this on phone: http://<tailnet-ip>:3210/` and separately reports public SSH, Tailnet ping, MagicDNS resolution, and port reachability

Remote operator example:

```bash
# on the client skillbox
cat >> .env <<'EOF'
SKILLBOX_SWIMMERS_EXPOSE=1
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
# default first-box onboarding uses the SPAPS auth/RBAC blueprint; pass BLUEPRINT=git-repo-http-service-bootstrap for a plain service
# resume a partial first-box/deploy failure without rebuilding the droplet
make box-up BOX=acme-prod PROFILE=dev-large DEPLOY_MANIFEST=clients/acme-prod/deploy.json RESUME=1
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

### Network Posture

Managed boxes default to `tailnet_only` posture: public SSH is a bootstrap
aperture that closes after Tailscale enrollment. See
[docs/tailnet-only-lifecycle.md](docs/tailnet-only-lifecycle.md) for the full
lifecycle, recovery paths, and posture verification commands.

```bash
python3 scripts/box.py posture-proof <box-id>          # verify lockdown
python3 scripts/box.py status <box-id> --format json   # includes violations
```

### Destructive Operation Guard

Destructive tools (`operator_teardown`, `operator_compose_down`) are gated by
a PreToolUse hook (`scripts/guard-destructive-op.sh`) that blocks execution
unless:

1. `dry_run=true` was passed (preview mode always passes)
2. All git repos in the workspace are committed and pushed
3. A dry-run was already executed this session

This prevents accidental infrastructure destruction with uncommitted work.

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

## When Not To Use `skillbox`

Do not use `skillbox` just because it is adjacent to a tool you already know.
Use something else when the real job is one of these:

- **A browser IDE product**: if the core experience needs to live in the
  browser, use something designed around that center of gravity.
- **Multi-user workspace fleets**: if you need tenancy, RBAC, policy layers,
  audit controls, or a hosted control plane, you are in Coder or Gitpod
  territory.
- **Untrusted-code sandboxing**: if isolation and ephemeral execution are the
  main job, look at Daytona, E2B, or similar sandbox/runtime systems.
- **Environment management only**: if you just need reproducible packages or
  shell environments, Devbox-like tooling is usually a better fit than a full
  box model.
- **Hosted SaaS ergonomics**: if you do not want to operate the host, Docker,
  and private access model yourself, `skillbox` is the wrong tool.

`skillbox` is best when the problem is narrower: one operator-owned machine that
should feel durable, legible, and agent-friendly.

## Installation

The public entrypoint is `install.sh`. It wraps the canonical `first-box`
flow: acquire or reuse a checkout, hydrate `.env`, initialize or reuse
`SKILLBOX_STATE_ROOT` (`./.skillbox-state` locally, `/srv/skillbox` on the
DigitalOcean target), attach or create private client config when requested,
prove readiness with `acceptance`, and open a client-ready surface under
`sand/<client>/`.

### Option 1: One-command installer

```bash
curl -fsSL https://raw.githubusercontent.com/build000r/skillbox/main/install.sh | bash -s -- --client personal
```

### Option 2: Local checkout

```bash
cp .env.example .env
make first-box CLIENT=personal
make build
make up
make shell
```

Or from an existing checkout:

```bash
bash install.sh --client personal
```

### Option 3: Existing Linux host or droplet

```bash
cp .env.example .env
cd scripts
sudo ./01-bootstrap-do.sh
sudo TAILSCALE_AUTHKEY="tskey-..." TAILSCALE_HOSTNAME="skillbox-dev" ./02-install-tailscale.sh
cd ..
make first-box CLIENT=personal
make build
make up
```

### Option 4: Operator-provisioned remote box

If the operator MCP server is configured, provision a box from Claude Code:

```bash
# MCP: operator_provision { box_id: "dev-01", profile: "dev-small", dry_run: true }
# Review dry-run output, confirm, then:
# MCP: operator_provision { box_id: "dev-01", profile: "dev-small" }
```

Or via make:

```bash
make box-up BOX=dev-01 PROFILE=dev-small
# default BLUEPRINT is git-repo-http-service-bootstrap-spaps-auth
make box-up BOX=dev-01 PROFILE=dev-small DEPLOY_MANIFEST=clients/dev-01/deploy.json RESUME=1
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
| `make doctor` | Validates the outer repo shell: manifests, Compose wiring, and the default `skill-repo-set` sync path |
| `make runtime-render` | Prints the resolved internal runtime graph |
| `make runtime-sync` | Creates managed repo/log directories, reconciles managed artifacts against declared pins or source files, and installs declared skills with generated lockfiles for the active core/client scope |
| `make runtime-status` | Summarizes declared repos, artifacts, skills, tasks, services, logs, and checks |
| `make runtime-skills` | Shows effective skill availability across global, client, and project-local layers, including broken/scope violations and shadowed declarations |
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
| `make box-up` | Provision a new remote box (DO + Tailscale) with the SPAPS auth/RBAC blueprint by default, or resume a partial provision with `RESUME=1` |
| `make box-down` | Tear down a remote box |
| `make box-status` | Health-check a remote box, including the phone URL, public SSH, Tailnet ping, MagicDNS, and swimmers port reachability |
| `make box-list` | List all boxes from inventory |
| `make box-ssh` | SSH into a remote box |
| `make box-profiles` | List available box profiles |

### Scripts

| Script | Purpose | Example |
|---|---|---|
| `scripts/01-bootstrap-do.sh` | Bootstrap a fresh Ubuntu or DigitalOcean host | `sudo ./scripts/01-bootstrap-do.sh` |
| `scripts/02-install-tailscale.sh` | Join the tailnet and harden SSH | `sudo TAILSCALE_AUTHKEY="tskey-..." ./scripts/02-install-tailscale.sh` |
| `scripts/04-reconcile.py render` | Print the resolved sandbox model | `python3 scripts/04-reconcile.py render --with-compose` |
| `scripts/04-reconcile.py doctor` | Run drift and readiness checks | `python3 scripts/04-reconcile.py doctor` |
| `scripts/05-swimmers.sh` | Manage the workspace-local swimmers install and process lifecycle | `./scripts/05-swimmers.sh status` |
| `scripts/operator_mcp_server.py` | Operator MCP server for fleet and container lifecycle | Runs via `.mcp.json` as `skillbox-operator` |
| `scripts/guard-destructive-op.sh` | PreToolUse hook gating destructive operator tools | Called automatically by Claude Code hooks |
| `.env-manager/manage.py context` | Generate CLAUDE.md and AGENTS.md from the resolved runtime graph | `python3 .env-manager/manage.py context --client personal` |
| `.env-manager/manage.py focus` | Activate a client with live state and enriched context | `python3 .env-manager/manage.py focus personal --format json` |
| `.env-manager/manage.py stewardship-report` | Build a client-scoped operator evidence packet with risks, proof, and not-assessed hardening gaps | `python3 .env-manager/manage.py stewardship-report personal --format md --write` |
| `.env-manager/manage.py parity-report` | Compare a client runtime graph against its production-stack parity contract | `python3 .env-manager/manage.py parity-report personal --format json` |
| `.env-manager/manage.py render` | Print the resolved internal runtime graph | `python3 .env-manager/manage.py render --format json` |
| `.env-manager/manage.py sync` | Create managed repo/artifact/log directories and install declared skills for the selected core/client scope | `python3 .env-manager/manage.py sync --client personal --dry-run` |
| `.env-manager/manage.py doctor` | Validate the internal repos/skills/logs/check graph for the selected core/client scope | `python3 .env-manager/manage.py doctor --client personal` |
| `.env-manager/manage.py status` | Summarize repo, artifact, skill, task, service, log, and health state for the selected core/client scope | `python3 .env-manager/manage.py status --client personal` |
| `.env-manager/manage.py pressure-report` | Read-only disk pressure, protected buckets, approved non-production worker target, and RCH/SBH posture | `python3 .env-manager/manage.py pressure-report --format json` |
| `.env-manager/manage.py rch-report` | Read-only Remote Compilation Helper worker/check/status/hook readiness; never installs hooks | `python3 .env-manager/manage.py rch-report --format json` |
| `.env-manager/manage.py sbh-report` | Read-only Storage Ballast Helper doctor/status/stats/blame posture with observe-first mutation gates | `python3 .env-manager/manage.py sbh-report --format json` |
| `.env-manager/manage.py skills` | Show the effective skill set for a cwd/client/profile, with global extras, broken links, scope violations, project-local layers, and shadowed lower-precedence sources | `python3 .env-manager/manage.py skills --client personal --profile local-all --cwd "$PWD"` |
| `.env-manager/manage.py mcp-audit` | Audit Claude/Codex MCP config parity; declared `kind:mcp` services (any profile) are intentional, only undeclared servers count as `unexplained_drift` | `python3 .env-manager/manage.py mcp-audit --cwd "$PWD" --format json` |
| `.env-manager/manage.py evidence` | Read-only runtime evidence packet (doctor, status, pressure, pulse, skills, MCP parity, git dirty, Beads pointer) with stable keys and explicit blocked/gray conditions; optionally `--write` an artifact | `python3 .env-manager/manage.py evidence --cwd "$PWD" --format json --write` |
| `.env-manager/manage.py mmdx` | Fuzzy-find and open local Mermaid/MMDX diagrams through the Buildooor diagrams viewer | `python3 .env-manager/manage.py mmdx --cwd "$PWD" skill review realms --no-open` |
| `.env-manager/manage.py bootstrap` | Sync runtime state and run declared bootstrap tasks in dependency order | `python3 .env-manager/manage.py bootstrap --client acme-studio --task app-bootstrap` |
| `.env-manager/manage.py up` | Sync runtime state, run any service-declared bootstrap tasks, and start manageable services, expanding declared `depends_on` prerequisites and waiting for healthchecks when present | `python3 .env-manager/manage.py up --profile surfaces --service api-stub` |
| `.env-manager/manage.py down` | Stop manageable services started by the runtime manager, stopping selected dependents before their prerequisites | `python3 .env-manager/manage.py down --profile surfaces --service api-stub` |
| `.env-manager/manage.py restart` | Restart manageable services for the selected core/client scope, preserving declared dependency order | `python3 .env-manager/manage.py restart --profile surfaces --service web-stub` |
| `.env-manager/manage.py logs` | Print recent log output for declared services | `python3 .env-manager/manage.py logs --profile surfaces --service api-stub --lines 80` |
| `.env-manager/manage.py client-init` | Scaffold a new client overlay, optionally applying a reusable blueprint for repos, services, and client-scoped connector declarations | `python3 .env-manager/manage.py client-init acme-studio --blueprint git-repo --set PRIMARY_REPO_URL=https://github.com/acme/app.git` |
| `.env-manager/manage.py first-box` | Canonical first-run path: attach the private repo, reuse or scaffold the client, run acceptance, and open `sand/<client>/` | `python3 .env-manager/manage.py first-box personal --profile connectors` |
| `.env-manager/manage.py private-init` | Attach or initialize the private client-config repo for this checkout and persist the local clients-root override | `python3 .env-manager/manage.py private-init --path ../skillbox-config` |
| `.env-manager/manage.py client-project` | Compile a single-client projection bundle with a client-safe runtime manifest and sanitized metadata | `python3 .env-manager/manage.py client-project personal --profile surfaces` |
| `.env-manager/manage.py client-open` | Build a client-safe working surface under `sand/<client>/` with scoped `CLAUDE.md`, `AGENTS.md`, and `.mcp.json`, or re-open an existing reviewed bundle via `--from-bundle` | `python3 .env-manager/manage.py client-open personal --profile connectors` |
| `.env-manager/manage.py client-diff` | Compare a client projection bundle against the current published payload and show both file-level and runtime-surface changes | `python3 .env-manager/manage.py client-diff personal --profile surfaces` |
| `.env-manager/manage.py client-publish` | Promote a client projection bundle into the attached private git repo under `clients/<client>/current/`, optionally persisting acceptance evidence | `python3 .env-manager/manage.py client-publish personal --acceptance --commit` |
| `.env-manager/pulse.py` | Pulse reconciliation daemon for continuous drift detection and auto-heal | `python3 .env-manager/pulse.py run --interval 30` |

### Pressure-aware builds and storage guard

When disk is tight or an agent is about to run expensive build/test commands,
start with the read-only pressure surface:

```bash
python3 .env-manager/manage.py pressure-report --format json
python3 .env-manager/manage.py rch-report --format json
python3 .env-manager/manage.py sbh-report --format json
python3 .env-manager/manage.py status --profile pressure-tools --format json --compact
```

The first approved worker target is `portfolio-devbox` on the Tailnet. The
policy explicitly excludes `jeremy`, `ssh-info`, and `sweet-potato-prod`; do not
use Sweet Potato production boxes for this lane.

RCH integration is fail-open and approval-gated. It may report safe probes such
as `rch --robot-triage --json`, `rch status --workers --jobs --json`,
`rch check --json`, and `rch hook status --json`, but `rch hook install`,
daemon setup, worker deployment, or source builds are separate mutation steps.

SBH integration starts in observe-first mode. `sbh doctor --pal`,
`sbh status --json`, `sbh stats --window 24h`, `sbh blame --json`, and
`sbh explain --id <decision-id>` are the intended visibility path. `sbh clean`,
`sbh protect`, service installation, ballast provision/release, and uninstall
are blocked until a follow-up approval explicitly allows mutation.

The current SBH Linux x86_64 canary pin is v0.4.22. Do not promote the
v0.4.23 Linux-named assets without a fresh file-type check: the
`sbh-v0.4.23-x86_64-unknown-linux-gnu.tar.xz` asset was rechecked on
2026-05-14, matched its published checksum, but extracted as a Mach-O arm64
binary rather than a Linux x86_64 executable.

Generated agent context, compact status, stewardship reports, and pulse state
include the same pressure/offload advisory. Protected buckets such as
`~/.codex`, `~/.claude`, and `~/.ssh` are hard no-touch paths; review-only
candidate caches are inventory, not an auto-delete list.

## Configuration

### Environment defaults

`.env.example` keeps the checkout source-only and points durable runtime state
at `SKILLBOX_STATE_ROOT`:

```dotenv
SKILLBOX_NAME=skillbox
SKILLBOX_STATE_ROOT=./.skillbox-state
SKILLBOX_WORKSPACE_ROOT=/workspace
SKILLBOX_REPOS_ROOT=/workspace/repos
SKILLBOX_SKILLS_ROOT=/workspace/skills
SKILLBOX_LOG_ROOT=/workspace/logs
SKILLBOX_HOME_ROOT=/home/sandbox
SKILLBOX_MONOSERVER_ROOT=/monoserver
SKILLBOX_CLIENTS_ROOT=/workspace/workspace/clients
SKILLBOX_CLIENTS_HOST_ROOT=./.skillbox-state/clients
SKILLBOX_MONOSERVER_HOST_ROOT=./.skillbox-state/monoserver
SKILLBOX_API_PORT=8000
SKILLBOX_WEB_PORT=3000
SKILLBOX_SWIMMERS_PORT=3210
SKILLBOX_SWIMMERS_PUBLISH_HOST=127.0.0.1
SKILLBOX_SWIMMERS_REPO=/monoserver/swimmers
SKILLBOX_SWIMMERS_INSTALL_DIR=/home/sandbox/.local/bin
SKILLBOX_SWIMMERS_BIN=/home/sandbox/.local/bin/swimmers
SKILLBOX_SWIMMERS_DOWNLOAD_URL=
SKILLBOX_SWIMMERS_DOWNLOAD_SHA256=
SKILLBOX_SWIMMERS_AUTH_MODE=
SKILLBOX_SWIMMERS_AUTH_TOKEN=
SKILLBOX_SWIMMERS_OBSERVER_TOKEN=
SKILLBOX_DCG_BIN=/home/sandbox/.local/bin/dcg
SKILLBOX_DCG_DOWNLOAD_URL=
SKILLBOX_DCG_DOWNLOAD_SHA256=
SKILLBOX_DCG_PACKS=core.git,core.filesystem
SKILLBOX_PULSE_INTERVAL=30
SKILLBOX_RCH_BIN=/home/sandbox/.local/bin/rch
SKILLBOX_RCHD_BIN=/home/sandbox/.local/bin/rchd
SKILLBOX_RCH_WORKER_BIN=/home/sandbox/.local/bin/rch-wkr
SKILLBOX_RCH_WORKERS_CONFIG=/home/sandbox/.config/rch/workers.toml
SKILLBOX_RCH_DOWNLOAD_URL=
SKILLBOX_RCH_DOWNLOAD_SHA256=
SKILLBOX_SBH_BIN=/home/sandbox/.local/bin/sbh
SKILLBOX_SBH_CONFIG=/home/sandbox/.config/sbh/config.toml
SKILLBOX_SBH_DOWNLOAD_URL=
SKILLBOX_SBH_DOWNLOAD_SHA256=
SKILLBOX_DCG_MCP_PORT=3220
SKILLBOX_FWC_BIN=/home/sandbox/.local/bin/fwc
SKILLBOX_FWC_DOWNLOAD_URL=
SKILLBOX_FWC_DOWNLOAD_SHA256=
SKILLBOX_FWC_MCP_PORT=3221
SKILLBOX_FWC_ZONE=work
SKILLBOX_FWC_CONNECTORS=github,slack,linear
SKILLBOX_DO_TOKEN=
SKILLBOX_DO_SSH_KEY_ID=
SKILLBOX_TS_AUTHKEY=
```

Canonical local state layout:

```text
.skillbox-state/
  clients/
  home/
    .claude/
    .codex/
    .local/
  logs/
  monoserver/
```

`SKILLBOX_SWIMMERS_REPO` is now an optional source checkout path. If you set
`SKILLBOX_SWIMMERS_DOWNLOAD_URL`, `make runtime-sync` or `make swimmers-install`
can hydrate the binary without needing `/monoserver/swimmers`.

`SKILLBOX_FWC_CONNECTORS` is the box-level connector superset. Client overlays
may declare only a narrowed `client.connectors` subset, and `doctor` plus
`acceptance` now fail before activation if a client tries to widen the box
contract.

The default `connectors` profile is runtime-only: install pinned connector
binaries and expose MCP services. If you need local FWC/DCG source checkouts
for inspection or development, add `--profile connectors-dev` explicitly.

`SKILLBOX_DO_TOKEN`, `SKILLBOX_DO_SSH_KEY_ID`, and `SKILLBOX_TS_AUTHKEY` are
required only for fleet management (`operator_provision` / `make box-up`).
Run `python3 scripts/box.py up <box-id> --profile dev-small --dry-run --format
json` first and check `credential_status`. If credentials are missing, add the
missing `KEY=value` lines to `.env.box` on the operator machine before running
real provisioning.

### Optional private repo split

The canonical local setup keeps client overlays and `/monoserver` content under
`.skillbox-state/`. If you want a separate private operator repo instead,
override the host-side persistence roots:

```text
~/repos/
  skillbox/                    # public engine and templates
  skillbox-config/             # private multi-client operator repo
    clients/
      personal/
      client-acme/
  client-acme-web/             # client code
  client-acme-api/             # client code
```

Recommended override in `skillbox/.env`:

```dotenv
SKILLBOX_CLIENTS_HOST_ROOT=../skillbox-config/clients
SKILLBOX_MONOSERVER_HOST_ROOT=..
```

Or let `private-init` write the clients-root override for you and initialize the
private repo in one step:

```bash
python3 .env-manager/manage.py private-init --path ../skillbox-config
```

Or use the canonical first-run flow:

```bash
python3 .env-manager/manage.py first-box personal
python3 .env-manager/manage.py first-box client-acme \
  --blueprint git-repo-http-service-bootstrap \
  --set PRIMARY_REPO_URL=https://github.com/acme/app.git \
  --set BOOTSTRAP_COMMAND='pnpm install && mkdir -p .skillbox && touch .skillbox/bootstrap.ok' \
  --set SERVICE_COMMAND='pnpm dev'
```

With that setup:

- `skillbox` stays publishable
- `skillbox-config/clients/<client>/overlay.yaml` is the private source of truth
- `skillbox-config/clients/<client>/skills/` holds private client-local skill sources
- `skillbox-config/clients/<client>/skill-repos.yaml` declares client-specific skill repos
- overlay scaffold artifacts are first-class: planning clients get `plans/`, while skill-builder clients get `workflows/`, `evaluations/`, `invocations/`, and `observability/`
- `client-init`, `sync`, and `focus` write client config into the private repo
- `client-diff` and `client-publish` default to the attached private repo unless you explicitly pass `--target-dir`
- `client-project <client>` compiles a client-safe bundle under `builds/clients/<client>/`
- `first-box <client>` is the one-command runtime setup path for attaching the private repo, activating the client, and writing `sand/<client>/`; if the overlay already exists, scaffold arguments such as `--blueprint` are reported as ignored unless `--force` is passed
- `client-open <client>` turns that bundle into a ready-to-work client surface under `sand/<client>/`, and `--from-bundle` re-opens a reviewed artifact without running live focus
- client application repos stay separate under the shared monoserver tree
- clients usually do not need access to the `skillbox` repo itself

### Client projection bundles

`client-project` is the first control-plane boundary in code, not just in
convention. It takes the public engine, the selected private client overlay,
and the active profiles, then emits a single-client bundle:

```bash
python3 .env-manager/manage.py client-project personal
python3 .env-manager/manage.py client-project personal --profile surfaces --output-dir ./builds/clients/personal-surfaces
```

The bundle currently includes:

- `workspace/runtime.yaml` with `selection.default_client` pinned to the selected client
- only the selected client's overlay, scaffold docs, source skill tree, and companion manifest or lock files
- only the skill bundles referenced by the selected runtime scope
- `runtime-model.json` with host paths and secret-like env keys removed
- `projection.json` with a deterministic file list and payload tree hash

The bundle does not include:

- other clients' overlays or bundled skills
- local secrets or host-only roots such as `SKILLBOX_CLIENTS_HOST_ROOT`

This is the intended handoff artifact when you want something client-specific
without exposing the full operator repo.

### Opening a client surface

`client-open` is the default operator entrypoint for a shared `skillbox/` plus
private multi-client `skillbox-config/` setup:

```bash
python3 .env-manager/manage.py client-open personal
python3 .env-manager/manage.py client-open client-acme --profile connectors
python3 .env-manager/manage.py client-open personal --output-dir ./artifacts/open-personal
python3 .env-manager/manage.py client-open personal --from-bundle ./builds/clients/personal
```

It does three things in one step:

- rebuilds the selected client's projection bundle into `sand/<client>/` by default
- writes client-scoped `CLAUDE.md` and `AGENTS.md` into that surface instead of your shared home context
- materializes the selected client's scaffold docs, local client skill sources, and resolved `context.yaml` inside that surface
- writes a filtered `.mcp.json` that includes only the MCP servers requested by the selected client and profiles

When you already have a reviewed bundle, `--from-bundle <dir>` skips live
`focus`, copies that bundle into the open surface, and regenerates static
context plus `.mcp.json` from the bundled `runtime-model.json`.

The generated surface is meant to be the safe place to point an agent at when
you want one client in scope and every other client out of scope.

### Publishing a client bundle

The hardened v1 promotion loop is:

```bash
python3 .env-manager/manage.py private-init --path ../skillbox-config
python3 .env-manager/manage.py client-init personal \
  --blueprint git-repo-http-service-bootstrap \
  --set PRIMARY_REPO_URL=https://github.com/acme/app.git \
  --set BOOTSTRAP_COMMAND='pnpm install && mkdir -p .skillbox && touch .skillbox/bootstrap.ok' \
  --set SERVICE_COMMAND='pnpm dev'
python3 .env-manager/manage.py sync --client personal --profile surfaces
python3 .env-manager/manage.py focus personal --profile surfaces --format json
python3 .env-manager/manage.py client-diff personal --profile surfaces
python3 .env-manager/manage.py client-publish personal --acceptance --commit --profile surfaces
```

`client-diff` compares a candidate bundle against the existing
`clients/<client>/current/` payload in the attached private repo. It shows:

- added, removed, and changed files in the bundle payload
- runtime-surface deltas across repos, artifacts, env files, skills, tasks,
  services, logs, and checks
- publish metadata drift, so you can see whether `publish.json` still matches
  the candidate bundle

`client-publish` then turns that reviewed bundle into the latest
client-facing artifact in a private git repo:

```bash
python3 .env-manager/manage.py client-publish personal --commit
python3 .env-manager/manage.py client-publish personal \
  --from-bundle ./builds/clients/personal
python3 .env-manager/manage.py client-publish personal \
  --acceptance --commit
```

If you need to publish somewhere other than the attached repo for a specific
run, pass `--target-dir /path/to/other-private-repo`.

What v1 does:

- validates the selected bundle before promotion
- diffs a candidate bundle against the current published payload before promotion
- writes the payload to `clients/<client>/current/`
- writes `clients/<client>/publish.json` with the latest published metadata
- optionally writes `clients/<client>/acceptance.json` with compact readiness evidence from `acceptance`
- optionally creates one local git commit in the target repo

If you want promotion to mean "reviewed and actually verified on this box",
use `--acceptance`. That runs the acceptance gate first and persists a compact
record of the accepted profiles, MCP surfaces, and source commit alongside the
published payload in the private repo.

What v1 does not do:

- push to a remote
- diff arbitrary historical publishes against each other
- restart services or deploy application code
- manage publish history beyond the latest `publish.json`

### Skill repo config

`workspace/skill-repos.yaml` declares where skills come from:

```yaml
version: 2

skill_repos:
  - repo: build000r/skills
    ref: main
    pick: [ask-cascade, audit-plans, build-vs-clone, commit, crap, describe, divide-and-conquer, domain-planner, domain-reviewer, domain-scaffolder, mutate, oss-doc-audit, reproduce, skill-issue]

  - path: ../skills
    pick: [dev-sanity, skillbox-operator]
```

Each entry is either a GitHub repo (cloned into `workspace/skill-repos/`) or a
local path (referenced directly). `sync` clones or fetches repos, filtered-copies
skill directories into `~/.claude/skills/` and `~/.codex/skills/`, and writes a
lock file with resolved commit SHAs. Private writing skills and Cass-backed
memory skills are intentionally outside the core pack; Cass-backed memory skills
live in `workspace/skill-repos-memory.yaml` and are installed only when the
`memory` profile is active.

`skill_repos` also supports distributor-backed sources for reviewed
multi-client skill delivery. A config can declare a top-level `distributors`
section and then select skills from a distributor entry:

```yaml
version: 3

distributors:
  - id: acme-skills
    url: https://skills.acme.dev/api/v1
    client_id: client-42
    auth:
      method: api-key
      key_env: ACME_DISTRIBUTOR_KEY
    verification:
      public_key: "ed25519:..."

skill_repos:
  - distributor: acme-skills
    pick: [deploy, codebase-audit]
    pin:
      deploy: 7
```

Distributor sync fetches a signed per-client manifest, verifies signed
`.skillbundle.tar.gz` bundles, installs the selected skills through the same
filtered-copy path as repo and local-path sources, and records distributor
metadata in the generated lockfile. This is a sync-time delivery channel only;
installed skill content remains local and usable offline after sync.

The local product loop is also available for dogfooding reviewed skill
releases without a hosted control plane:

```bash
python3 .env-manager/manage.py distribution-publish ./path/to/skill \
  --version 1 \
  --manifest-path ./dist/manifest.json \
  --artifact-root ./dist/artifacts \
  --signing-key file:./dist/signing-key.pem \
  --distributor-id local-skills \
  --client-id client-42 \
  --target box \
  --capability deploy \
  --changelog "Initial release"

python3 .env-manager/manage.py distribution-preview \
  --manifest-path ./dist/manifest.json \
  --public-key "ed25519:..." \
  --distributor-id local-skills \
  --state-root ./.skillbox-state \
  --pick deploy \
  --format json

python3 .env-manager/manage.py sync --dry-run
python3 .env-manager/manage.py sync

python3 .env-manager/manage.py distribution-rollback \
  --manifest-path ./dist/manifest.json \
  --public-key "ed25519:..." \
  --distributor-id local-skills \
  --skill deploy \
  --version 1 \
  --state-root ./.skillbox-state \
  --install-target ~/.claude/skills \
  --lockfile ./workspace/skill-repos.lock.json \
  --reason "bad update"
```

Manifests can use `schema_version: 2` with per-version `artifacts[]`.
Preview is read-only: it verifies the signed manifest, resolves client pins and
manifest floors to a concrete artifact, reports cache/signature state, and
blocks missing selected artifacts before sync. Sync consumes the same selected
artifact metadata, so a client pin below the recommendation downloads and
installs the pinned artifact rather than the recommended one. Rollback uses the
verified bundle cache and records `pinned_by=rollback` in the lockfile.

Client overlays declare their own `skill-repos.yaml` under
`${SKILLBOX_CLIENTS_HOST_ROOT:-./workspace/clients}/<client>/skill-repos.yaml`.

### Effective skill visibility

`manage.py skills` is the conflict-aware view of what an agent can use for a
given cwd, client, and profile:

```bash
python3 .env-manager/manage.py skills --client personal --profile local-all --cwd "$PWD"
```

It layers declared default skills, matched client skills, project-local
`.claude/skills` or `.codex/skills`, and the operator's global
`~/.claude/skills` plus `~/.codex/skills`. The output shows one effective
winner per skill, counts lower-precedence shadowed sources, and flags broken
global symlinks, broken project-local links, undeclared global extras, skills
installed outside their declared scope, and skills that are expected for the
current cwd but are not available yet.

Use `--issues-only` for a compact agent/operator check, and `--show-sources`
when you intentionally want the larger inventory of source-tree skills that are
not currently synced. The `sbp candidates` wrapper is the short exploratory
path for that larger inventory when a clean policy check is not enough and the
agent needs to consider every linkable source skill.

Use `skill-audit` when the question is broader than the current cwd. It scans
the configured downstream repo roots from `skill-scope.yaml`, reports repo-local
missing skills and scope violations per repo, and summarizes global drift once
so agents do not repeat the same global warning for every checkout.

```bash
python3 .env-manager/manage.py skill-audit --cwd "$PWD"
python3 .env-manager/manage.py skill-audit --scan-root ~/repos --limit 20
```

Use `mcp-audit` when the question is whether the same repo exposes the right
MCP servers to both agent runtimes. It reads Claude Code project config from
`.mcp.json`, Codex project config from `.codex/config.toml`, and reports
missing, extra, disabled, invalid, and Claude-only/Codex-only MCP entries.

```bash
python3 .env-manager/manage.py mcp-audit --cwd "$PWD"
sbp mcp
```

An operator config root may also define `skill-scope.yaml` or
`skills-scope.yaml` beside `clients/`. Those files declare repo-only or
global-allowed skill rules; `manage.py skills` reports `scope_violations` when
a skill is installed outside its declared availability. The policy can also
define named `project_categories` so rules can say `categories: [frontend]`
instead of repeating the same repo path list in every rule.

The same view is available to agents as the read-only `skillbox_skills` MCP
tool. Run it before adding, moving, or globally installing skills so the agent
can keep global utilities global and project/category skills local to the repos
where they belong.

Use the singular `skill` command when you want to apply that policy:

```bash
python3 .env-manager/manage.py skill plan mcp-server-design --cwd ~/repos/opensource/skillbox
python3 .env-manager/manage.py skill add mcp-server-design --cwd ~/repos/opensource/skillbox
python3 .env-manager/manage.py skill activate mcp-server-design --cwd ~/repos/opensource/skillbox
python3 .env-manager/manage.py skill add ui --to category --category frontend
python3 .env-manager/manage.py skill sync --cwd "$PWD" --dry-run
python3 .env-manager/manage.py skill prune --cwd "$PWD" --from project --dry-run
python3 .env-manager/manage.py skill prune --from global --dry-run
python3 .env-manager/manage.py skill-audit --cwd "$PWD"
python3 .env-manager/manage.py overlay activate marketing --cwd "$PWD"
```

`skill add` links the source skill into the selected global or repo-local
Claude/Codex skill roots. `--to auto` follows `skill-scope.yaml`: global
allowlist skills go into `~/.claude/skills` and `~/.codex/skills`; scoped
skills go under the matching repo/category as `.claude/skills` and
`.codex/skills`. `skill activate` performs the same link operation and also
prints an activation packet containing the source `SKILL.md`, so the current
agent session can use the skill immediately while future Claude and Codex
sessions discover the filesystem links normally. `overlay activate <name>` is
the cwd-scoped hot path: it does not persist overlay state, links literal
overlay-scoped skills into the current repo's `.claude/skills` and
`.codex/skills`, and returns one activation packet per linked skill. Pass
`--to global` only when you intentionally want operator-wide links in
`~/.claude/skills` and `~/.codex/skills`; use `overlay on` for persistent
overlay state. `overlay off <name>` unlinks cwd-local overlay symlinks by
default; pass `--scope global` or `--scope all` for wider cleanup. `remove`,
`move`, and `prune` require `--dry-run` first or `--yes` to apply unlinking
actions.

The repo-owned `scripts/sbp` and `scripts/sbo` wrappers delegate to the same
lifecycle surface. Install them as `~/.local/bin/sbp` and `~/.local/bin/sbo`
with `make wrappers-install`, or pass `--install-wrappers` to `install.sh`
when provisioning a checkout.

```bash
make wrappers-install
sbp
sbp help
sbp skills --issues-only
sbp candidates --json
sbp recalibrate
sbp mcp
sbp recalibrate --fleet --limit 20
sbp skills audit --limit 20
sbp skill plan mcp-server-design
sbp skill add mcp-server-design
sbp skill activate mcp-server-design
sbp overlay activate marketing
sbp skill add ui --to category --category frontend
sbp skill sync --dry-run
sbp mmdx review
sbp mmdx skill_review_realms/review --no-open
sbp launch ../api ../web --request "Audit auth drift" --dry-run
sbo mmdx review
```

`sbp` with no args is intentionally read-only: it prints a fast cwd-aware skill
home view and next commands, skipping the slower global scan. Skill and overlay
commands infer the active client from the downstream cwd unless
`SKILLBOX_CLIENT` is set; wrapper runtime service commands pass the downstream
cwd through for the same client inference. Direct `manage.py` runtime calls
still use the configured default unless you pass `--client` or `--cwd`.
Runtime mutation is explicit with
`sbp up`, `sbp down`, `sbp restart`, or `sbp skill ...`; use
`sbp skills --issues-only` for the full global/project drift check and
`sbp candidates` for the full cwd-local linkable source inventory before
bucketing candidates into definitely, maybe, and no. Use `sbp recalibrate` for
the cwd-local add/remove recommendation, and
`sbp skills audit` or `sbp recalibrate --fleet` for the cross-repo map.
`sbp recalibrate` prints the dry-run sync/prune commands for the current repo
and the Claude/Codex MCP parity check; the fleet form prints the narrow dry-run
commands to review before applying repo-local, global, or overlay cleanup.
`sbp mcp` runs only the read-only MCP audit. `sbp mmdx` is the short path for
downstream Mermaid/MMDX diagrams: it fuzzy-matches from the current repo and
opens the selected file through the Buildooor diagrams viewer, while
`--no-open` lists or resolves candidates without launching a browser.

`sbp launch` and its `sbp bulk` alias are Swimmers batch launchers for agents:
pass one or more directories plus `--request` (or `--request-file`) and they
POST to the running Swimmers `/v1/sessions/batch` API, creating one agent
session per directory. Use `--dry-run --json` first to inspect the exact
payload; set `--base-url` or `SWIMMERS_TUI_URL` for a non-default Swimmers API.

Agents can apply the same flow through the `skillbox_skill` and
`skillbox_overlay` MCP tools after a dry-run review. For diagrams, use
`skillbox_mmdx_open` with `open=false` to resolve/list candidates before
launching the viewer.

### Generated skill lockfiles

`make runtime-sync` writes `workspace/skill-repos.lock.json` for the shared
default skill set, and client overlays write their own lockfiles under
`${SKILLBOX_CLIENTS_HOST_ROOT:-./workspace/clients}/<client>/skill-repos.lock.json`.

Each lockfile records:

- the config file SHA (detects when the config changed since last sync)
- the resolved commit SHA per skill (what ref was installed)
- the installed tree SHA per skill (for drift detection)
- distributor manifest versions, bundle hashes, selected versions, and pin
  reasons for distributor-backed skills

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
      kind: skill-repo-set
      skill_repos_config: ${SKILLBOX_WORKSPACE_ROOT}/workspace/skill-repos.yaml
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

Client overlays are auto-discovered from
`${SKILLBOX_CLIENTS_HOST_ROOT:-./.skillbox-state/clients}/<client>/overlay.yaml` and
always resolve to `${SKILLBOX_CLIENTS_ROOT}` inside the box. That lets you keep
the public engine repo and the private client-config repo separate while the
runtime still sees a stable in-box path.
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
      bundle_dir: ${SKILLBOX_CLIENTS_ROOT}/personal/bundles
      manifest: ${SKILLBOX_CLIENTS_ROOT}/personal/skills.manifest
```

Create a new overlay scaffold with:

```bash
python3 .env-manager/manage.py client-init acme-studio
```

That base scaffold seeds the client-local planning pack
(`domain-planner`, `domain-reviewer`, `domain-scaffolder`, and
`divide-and-conquer`) plus `plans/INDEX.md` and `plans/{draft,released,sessions}`.

For workflow-only skill-builder clients, start from the built-in FWC-oriented
blueprint instead:

```bash
python3 .env-manager/manage.py client-init acme-builder \
  --blueprint skill-builder-fwc \
  --set CONNECTORS=github,slack
```

That blueprint switches the scaffold pack to `skill-builder`, seeds the
client-local `skill-issue` and `prompt-reviewer` sources and bundles, creates
`workflows/INDEX.md`, `workflows/EXTRACTION.md`, `evaluations/README.md`,
`invocations/README.md`, and `observability/README.md`, and declares matching
client log lanes. `client-project` and `client-open` carry that editable
surface into `sand/<client>/`, and `client-open` also writes a resolved
`context.yaml` there for the sandboxed agent.

For mixed clients that need both released plans and a client-local skill/report
loop, set `client.scaffold.pack: hybrid`. The hybrid pack preserves the
planning surface, seeds the workflow-builder directories, and installs both the
planning skills and the client-local `skill-issue` / `prompt-reviewer` pair.

For the hardened v1 onboarding path, start from the built-in blueprint that
wires repos, services, logs, bootstrap, and checks into the scaffold:

```bash
python3 .env-manager/manage.py client-init --list-blueprints
python3 .env-manager/manage.py client-init acme-studio \
  --blueprint git-repo-http-service-bootstrap \
  --set PRIMARY_REPO_URL=https://github.com/acme/app.git \
  --set BOOTSTRAP_COMMAND='pnpm install && pnpm prisma migrate deploy && mkdir -p .skillbox && touch .skillbox/bootstrap.ok' \
  --set SERVICE_COMMAND='pnpm dev'
```

If the client repo needs local SPAPS auth and RBAC fixtures on day one, use the
SPAPS-aware variant instead:

```bash
python3 .env-manager/manage.py client-init acme-studio \
  --blueprint git-repo-http-service-bootstrap-spaps-auth \
  --set PRIMARY_REPO_URL=https://github.com/acme/app.git \
  --set BOOTSTRAP_COMMAND='pnpm install && mkdir -p .skillbox && touch .skillbox/bootstrap.ok' \
  --set SERVICE_COMMAND='pnpm dev'
```

Every skillbox image installs `spaps@0.7.7` as a mandatory workspace CLI. The
SPAPS blueprint uses that installed `spaps` command by default; override
`SPAPS_CLI_COMMAND` only when you intentionally want a different command, such
as `npx --yes spaps@x.y.z`.

For a remote box that should be usable from your tailnet browser, set the
browser-facing URLs explicitly. For example, with a Tailnet IP of
`<tailnet-ip>` and an app on `5173`, bind the app to that Tailnet host instead
of a wildcard or loopback interface:

```bash
--set SERVICE_COMMAND='npm run dev -- --host <tailnet-ip> --port 5173' \
--set SERVICE_HEALTHCHECK_URL=http://<tailnet-ip>:5173/ \
--set SPAPS_AUTH_BASE_URL=http://<tailnet-ip>:5173 \
--set SPAPS_FIXTURE_BASE_URL=http://<tailnet-ip>:5173 \
--set SPAPS_BROWSER_API_URL=http://<tailnet-ip>:3301 \
--set SPAPS_CORS_ALLOW_ORIGINS=http://<tailnet-ip>:5173,http://localhost:5173,http://127.0.0.1:5173
```

`box up` fills those SPAPS browser/CORS values from the enrolled Tailscale IP
when the default SPAPS blueprint is used; pass explicit `--set` values only when
you need to override the derived URLs.

That scaffold adds:

- a managed `auth-api` service on `http://127.0.0.1:3301/health`
- a repo-local SPAPS fixture bootstrap task
- an app service that depends on auth before startup
- the preinstalled `spaps` CLI, so default first-box boot does not depend on an
  npm first-run install prompt
- a fixture postprocess step that rewrites generated browser artifacts to the
  configured `SPAPS_BROWSER_API_URL`, so remote browser sessions do not get
  stuck calling `localhost:3301`

Blueprints keep the default client scaffold unless they explicitly set a
different scaffold pack. They append client-scoped repos, artifacts, tasks,
services, logs, and checks to the generated overlay, so the next `render`,
`sync`, `bootstrap`, `status`, `doctor`, or `up` command already has something
concrete to operate on.

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
- the connectors profile is box-owned; blueprints may scaffold only client-level
  connector subsets under `client.connectors`
- `connectors-dev` is separate and only for optional FWC/DCG source checkouts
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
            │ .skillbox-state/     │
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
│ /home/sandbox/.claude │
│ /home/sandbox/.codex  │
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
│ skill-repos.yaml → clone + install      │
└───────────────────┬─────────────────────┘
                    │
                    ▼
       ┌──────────────────────────────────┐
       │ managed box internals            │
       ├──────────────────────────────────┤
       │ repos, artifacts, skills, checks │
       │ api/web stub health probes       │
       │ cloned skill repos + lockfiles   │
       │ runtime log (runtime.log)        │
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
| `skillbox_skills` | Effective skill availability, scope violations, and install/move recommendations |
| `skillbox_skill` | Plan/apply skill add, move, remove, prune, and policy sync actions |
| `skillbox_sync` | Sync state (create dirs, install skills) |
| `skillbox_up` / `skillbox_down` | Start/stop services |
| `skillbox_logs` | Recent service log output |
| `skillbox_bootstrap` | Run declared bootstrap tasks |
| `skillbox_focus` | Activate a client with live state and enriched context |
| `skillbox_onboard` | Scaffold and bootstrap a new client |
| `skillbox_client_init` | Create a new client overlay from blueprint |
| `skillbox_client_diff` | Review the delta between a candidate client bundle and the current published payload |
| `skillbox_pulse` | Query pulse daemon status |
| `skillbox_session_start` / `skillbox_session_event` / `skillbox_session_end` | Manage durable client-scoped session timelines |
| `skillbox_session_resume` / `skillbox_session_status` | Resume or inspect durable session state |
| `skillbox_worker_submit` / `skillbox_worker_status` | Submit open-ended worker tasks and poll broker state |
| `skillbox_worker_artifacts` / `skillbox_worker_promote_learning` | Read terminal run artifacts and explicitly promote reviewed learning proposals |

Opened client surfaces always include the `skillbox` MCP. Core surfaces also
include `cm` for procedural memory, and connector-capable surfaces add `fwc`
and `dcg` on top of that.

### Outside the box (operator tools)

`scripts/operator_mcp_server.py` runs on the operator machine and provides
fleet lifecycle tools. See the [Fleet Management](#fleet-management) section.

## Troubleshooting

### `make doctor` fails on Compose validation

Check that Docker is installed and `docker compose config --format json` works on the host.

### `make dev-sanity` warns about missing log directories or managed skill installs

That is expected on a fresh clone. The core runtime graph declares
`.skillbox-state/logs/runtime`, `.skillbox-state/logs/repos`, and the managed skill install roots plus
lockfile are also created on demand.

Run:

```bash
make runtime-sync
make dev-sanity
```

### `make up-surfaces` starts but ports are unreachable

The API and web surfaces bind to `127.0.0.1` by design. Use local forwarding or a host shell, not a public interface.

### Skill sync fails

Run:

```bash
python3 .env-manager/manage.py sync --dry-run --format json
python3 .env-manager/manage.py doctor --format json
```

Check for `SKILL_REPO_UNREACHABLE` (auth/network) or `SKILL_NOT_FOUND_IN_REPO` (bad pick list).
First-box acceptance also runs a `skill-availability` preflight after sync; if
it fails, declared skills are not installed cleanly into both managed
`~/.claude/skills` and `~/.codex/skills` roots.

### SSH login says identity is unknown

The shared SSH login hook no longer prints a warning for every non-interactive
SSH command. For an interactive diagnostic, set `SKILLBOX_LOGIN_WARN_IDENTITY=1`
and reconnect; then check `SSH_CLIENT`, `tailscale whois <client-ip>`, and
Tailnet reachability.

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
make runtime-sync
make doctor
```

### Pulse daemon won't start

Check if it's already running:

```bash
make pulse-status
```

If the PID file is stale (process died without cleanup), remove it from the
state root:

```bash
rm .skillbox-state/logs/runtime/pulse.pid
make pulse-start
```

### Operator tools are blocked by the guard hook

The destructive-op guard requires:
1. All git repos committed and pushed
2. A `dry_run=true` call before the real operation

Run `/commit`, push, then re-run with `dry_run: true` first.

## Limitations

- This is not a hosted control plane or a multi-user workspace platform.
- Skill distribution is still private and explicit: local publisher, preview,
  sync, and rollback primitives are implemented, but a hosted distributor
  service, standalone laptop CLI, background update checks, and short-lived
  token exchange are still future work.
- The API and web surfaces are inspection stubs, not a full UI.
- The internal runtime manager now does dependency-aware task and service orchestration plus managed env hydration, but it still does not try to replace app-specific deployment systems or CI.
- Secrets management and app-specific bootstrap details beyond what you declare in your overlays and blueprints are still your responsibility.
- The pulse daemon is single-process; it does not survive container restarts unless declared as a managed service in `runtime.yaml`.
- Fleet management requires DigitalOcean and Tailscale credentials. Other cloud providers are not supported.
- There is no license file in this repo yet. Add one before publishing it as open source.

## FAQ

### Popular Related Tools & Apps

These come up often because they sit near `skillbox`, but they do not all solve
the same layer of the problem. For the deeper positioning thesis, see
[docs/VISION.md](docs/VISION.md).

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
pushed, and a dry-run was already executed this session.

## About Contributions

> *About Contributions:* Please don't take this the wrong way, but I do not accept outside contributions for any of my projects. I simply don't have the mental bandwidth to review anything, and it's my name on the thing, so I'm responsible for any problems it causes; thus, the risk-reward is highly asymmetric from my perspective. I'd also have to worry about other "stakeholders," which seems unwise for tools I mostly make for myself for free. Feel free to submit issues, and even PRs if you want to illustrate a proposed fix, but know I won't merge them directly. Instead, I'll have Claude or Codex review submissions via `gh` and independently decide whether and how to address them. Bug reports in particular are welcome. Sorry if this offends, but I want to avoid wasted time and hurt feelings. I understand this isn't in sync with the prevailing open-source ethos that seeks community contributions, but it's the only way I can move at this velocity and keep my sanity.

## License

No license file is included yet.
