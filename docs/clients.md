# Clients

This page contains content moved from `README.md` during the README front-door split.

## Installation

The public entrypoint is `install.sh`. It wraps the canonical `first-box`
flow: acquire or reuse a checkout, hydrate `.env`, initialize or reuse
`SKILLBOX_STATE_ROOT` (`./.skillbox-state` locally, `/srv/skillbox` on the
DigitalOcean target), attach or create private client config when requested,
prove readiness with `acceptance`, and open a client-ready surface under
`sand/<client>/`.

### Option 1: One-command installer

```bash
curl -fsSL "https://raw.githubusercontent.com/build000r/skillbox/main/install.sh?$(date +%s)" | bash -s -- --client personal
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
