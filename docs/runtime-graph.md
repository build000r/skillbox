# Runtime Graph

This page contains content moved from `README.md` during the README front-door split.

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

The cut-over contract that backs this path is declared in the selected private
client overlay. A typical `local-core` overlay contains an API service, one or
more dependent backend services, and a browser-facing frontend. Each row
declares its repo root, dependencies, health URL, and supported modes.

Do not infer coverage for adjacent local surfaces from prose docs. Their
current lifecycle behavior comes from the selected client's `parity_ledger`;
only rows marked `covered` are managed by the normal runtime lifecycle.

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
[Tailnet App Addressing](tailnet-ingress.md) for the browser URL contract,
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
| `LOCAL_RUNTIME_PORT_MISMATCH` | Started or reused service process did not bind its declared runtime port |
| `LOCAL_RUNTIME_SERVICE_DEFERRED` | Requested surface is in the parity ledger but not covered |
| `LOCAL_RUNTIME_MODE_UNSUPPORTED` | Start mode not declared for a requested service |
| `LOCAL_RUNTIME_COVERAGE_GAP` | Declared ledger and covered set have drifted |
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
## Architecture

See [docs/ARCHITECTURE.md](ARCHITECTURE.md) for the full architecture map.

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
