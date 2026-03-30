# skillbox

> A thin, self-hosted Tailnet dev box for AI-assisted coding on a Dockerized droplet.

![runtime](https://img.shields.io/badge/runtime-Docker-2496ED?style=flat-square&logo=docker&logoColor=white)
![access](https://img.shields.io/badge/access-Tailscale-242424?style=flat-square&logo=tailscale&logoColor=white)
![shape](https://img.shields.io/badge/shape-thin%20starter-6E7781?style=flat-square)
![doctor](https://img.shields.io/badge/doctor-manifest%20checks-2ea44f?style=flat-square)

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

Most remote dev setups overshoot the need. You want one private box, one primary workspace container, your own Claude or Codex home directories, a few curated repos, and some pinned skills. You do not want to adopt a full hosted workspace control plane just to get there.

### The Solution

`skillbox` gives you a cloneable starter for a Tailnet-first dev box:

- SSH to the host over Tailscale
- run one main Docker workspace container
- mount `home/.claude` and `home/.codex` into that box
- declare the inside of the box with a runtime graph for repos, installed skills, services, logs, and checks
- pin and package default skills locally
- validate outer drift with `make doctor` and inner drift with `make dev-sanity`

### Why Use `skillbox`?

| Need | `skillbox` answer |
|---|---|
| Private access without public SSH exposure | Tailscale host access plus host hardening scripts |
| A workspace that feels like a narrowed local setup | One bind-mounted `/workspace` with `repos/`, `skills/`, `logs/`, and home mounts |
| A sane way to let the box grow over time | `workspace/runtime.yaml` plus `.env-manager/manage.py` manage internal repos, installed skills, logs, and checks |
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
- optional API and web inspection surfaces
- packaged default `.skill` bundles under `default-skills/`
- installed default skills under `home/.claude/skills` and `home/.codex/skills`

## Design Philosophy

### 1. Thin beats magical

This repo intentionally stops well short of Coder or Daytona. It focuses on one operator-controlled box and a small set of explicit scripts.

### 2. Host SSH, container work

SSH lands on the host. Docker Compose runs the workspace and optional surfaces. The container is where your day-to-day work should feel familiar.

### 3. Declarative enough to check, not so abstract it disappears

`workspace/sandbox.yaml`, `workspace/dependencies.yaml`, `workspace/runtime.yaml`, and the skill manifests describe the intended box. `make doctor` checks the outer shell, and `make dev-sanity` checks the interior graph plus managed skill install state.

### 4. Portable skill packaging matters

The default skill packaging chain is vendored locally. A fresh clone does not need a sibling `../opensource` checkout just to package default skills.

### 5. Local-first operator ergonomics

The repo includes enough surfaces to inspect and validate the shape, but not so much that it becomes a platform you have to operate before you can work.

### 6. The box should describe its internals, not just its container

The new internal `.env-manager` layer is intentionally small. It does not try to become a second platform; it gives the box one declared source of truth for repos, installed skills, services, logs, and sanity checks so the workspace can accrete without turning into guesswork.

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

### Option 3: Copy into an existing repo workspace

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
| `make runtime-sync` | Creates managed repo/log directories and installs declared default skills with a generated lockfile |
| `make runtime-status` | Summarizes declared repos, skills, services, logs, and checks |
| `make dev-sanity` | Validates the internal runtime graph, filesystem readiness, and managed skill integrity |
| `make build` | Builds the workspace image |
| `make up` | Starts the workspace container |
| `make up-surfaces` | Starts the API and web stub surfaces |
| `make down` | Stops all containers |
| `make shell` | Opens a shell inside the workspace container |
| `make logs` | Tails compose logs |

### Scripts

| Script | Purpose | Example |
|---|---|---|
| `scripts/01-bootstrap-do.sh` | Bootstrap a fresh Ubuntu or DigitalOcean host | `sudo ./scripts/01-bootstrap-do.sh` |
| `scripts/02-install-tailscale.sh` | Join the tailnet and harden SSH | `sudo TAILSCALE_AUTHKEY="tskey-..." ./scripts/02-install-tailscale.sh` |
| `scripts/03-skill-sync.sh` | Resolve, stage, validate, and package default skills | `./scripts/03-skill-sync.sh --dry-run` |
| `scripts/04-reconcile.py render` | Print the resolved sandbox model | `python3 scripts/04-reconcile.py render --with-compose` |
| `scripts/04-reconcile.py doctor` | Run drift and readiness checks | `python3 scripts/04-reconcile.py doctor` |
| `.env-manager/manage.py render` | Print the resolved internal runtime graph | `python3 .env-manager/manage.py render --format json` |
| `.env-manager/manage.py sync` | Create managed repo/log directories and install declared default skills | `python3 .env-manager/manage.py sync --dry-run` |
| `.env-manager/manage.py doctor` | Validate the internal repos/skills/logs/check graph | `python3 .env-manager/manage.py doctor` |
| `.env-manager/manage.py status` | Summarize repo, skill, service, log, and health state | `python3 .env-manager/manage.py status` |

## Configuration

### Environment defaults

`.env.example` sets the main runtime paths and ports:

```dotenv
SKILLBOX_NAME=skillbox
SKILLBOX_WORKSPACE_ROOT=/workspace
SKILLBOX_REPOS_ROOT=/workspace/repos
SKILLBOX_SKILLS_ROOT=/workspace/skills
SKILLBOX_LOG_ROOT=/workspace/logs
SKILLBOX_HOME_ROOT=/home/sandbox
SKILLBOX_API_PORT=8000
SKILLBOX_WEB_PORT=3000
```

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

### Generated skill lockfile

`make runtime-sync` now writes `workspace/default-skills.lock.json`, which records:

- the current manifest and sources-config digests
- the bundle digests for each declared default skill
- the installed tree hashes for the managed Claude and Codex skill homes

The lockfile is generated state and is gitignored, so running sync does not
turn normal local runtime reconciliation into noisy repo dirt.

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
```

### Runtime graph

`workspace/runtime.yaml` declares the inside of the box:

```yaml
version: 1

repos:
  - id: skillbox-self
    kind: repo
    path: ${SKILLBOX_WORKSPACE_ROOT}
    source:
      kind: bind
      path: ${ROOT_DIR}
    sync:
      mode: external

  - id: managed-repos
    kind: workspace-root
    path: ${SKILLBOX_REPOS_ROOT}
    source:
      kind: directory
    sync:
      mode: ensure-directory

skills:
  - id: default-skills
    kind: packaged-skill-set
    bundle_dir: ${SKILLBOX_WORKSPACE_ROOT}/default-skills
    manifest: ${SKILLBOX_WORKSPACE_ROOT}/workspace/default-skills.manifest
    sources_config: ${SKILLBOX_WORKSPACE_ROOT}/workspace/default-skills.sources.yaml
    lock_path: ${SKILLBOX_WORKSPACE_ROOT}/workspace/default-skills.lock.json
    sync:
      mode: unpack-bundles
    install_targets:
      - id: claude
        path: ${SKILLBOX_HOME_ROOT}/.claude/skills
      - id: codex
        path: ${SKILLBOX_HOME_ROOT}/.codex/skills

services:
  - id: internal-env-manager
    kind: orchestration
    path: ${SKILLBOX_WORKSPACE_ROOT}/.env-manager
    command: python3 .env-manager/manage.py
    log: runtime

logs:
  - id: runtime
    path: ${SKILLBOX_LOG_ROOT}/runtime

checks:
  - id: runtime-manager
    type: path_exists
    path: ${SKILLBOX_WORKSPACE_ROOT}/.env-manager/manage.py
```

### Workload Profiles

`workspace/runtime.yaml` can now describe named workload slices with `profiles`.
The runtime manager treats `core` as the shared baseline and lets you activate
additional slices when you render, sync, inspect, or sanity-check the box.

Examples:

```bash
python3 .env-manager/manage.py render --profile bookme
python3 .env-manager/manage.py sync --profile bookme
python3 .env-manager/manage.py status --profile bookme
python3 .env-manager/manage.py doctor --profile bookme

make runtime-sync PROFILE=bookme
make runtime-status PROFILE=bookme
make dev-sanity PROFILE=bookme
```

That makes `skillbox` behave less like one static starter and more like a
personal environment compiler: one repo can declare multiple real project
slices without forcing every repo, service, log, and check to exist all the
time.

## Architecture

```text
                Tailscale SSH
                      |
                      v
            +----------------------+
            |   Host machine       |
            |  Ubuntu / Docker     |
            |----------------------|
            | scripts/01,02        |
            | docker-compose.yml   |
            +----------+-----------+
                       |
          +------------+------------+
          |                         |
          v                         v
+-------------------+      +-------------------+
| workspace         |      | optional surfaces |
|-------------------|      |-------------------|
| /workspace        |      | api :8000         |
| /workspace/repos  |      | web :3000         |
| /workspace/skills |      +-------------------+
| /workspace/logs   |
| /home/.claude     |
| /home/.codex      |
+---------+---------+
          |
          v
+-----------------------------------------+
| declarative control layers              |
|-----------------------------------------|
| workspace/sandbox.yaml                  |
| workspace/dependencies.yaml             |
| workspace/runtime.yaml                  |
| 04-reconcile.py                         |
| .env-manager/manage.py                  |
| 03-skill-sync.sh / package_skill.py     |
+-------------------+---------------------+
                    |
                    v
       +----------------------------------+
       | managed box internals            |
       |----------------------------------|
       | repos, installed skills, checks  |
       | api/web stub health probes       |
       | default skill bundles + lockfile |
       +----------------------------------+
```

## Troubleshooting

### `make doctor` fails on Compose validation

Check that Docker is installed and `docker compose config --format json` works on the host.

### `make dev-sanity` warns about missing log directories or managed skill installs

That is expected on a fresh clone. The runtime graph declares `logs/runtime` and `logs/repos`, and the managed skill install roots plus lockfile are also created on demand.

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

The internal runtime manager evaluates host paths that correspond to the container's `/workspace/...` tree.

Run:

```bash
make runtime-sync
make runtime-status
```

If a repo is still missing after sync, the runtime entry is probably configured with `sync.mode: external` and expects a bind mount or manual clone.

### Default skills look stale

Re-run:

```bash
./scripts/03-skill-sync.sh
make runtime-sync
make doctor
```

## Limitations

- This is not a hosted control plane or a multi-user workspace platform.
- There is no release installer, package manager distribution, or cloud provisioning flow yet.
- The API and web surfaces are inspection stubs, not a full UI.
- The internal runtime manager currently handles declaration, sync, status, and sanity checks. It does not yet do full per-repo start/stop orchestration.
- Secrets management and richer per-project bootstrap workflows are still your responsibility.
- There is no license file in this repo yet. Add one before publishing it as open source.

## FAQ

### Is SSH supposed to go to the host or the container?

The host. The container is the workspace runtime, not the SSH target.

### Why keep `home/.claude` and `home/.codex` in the repo?

So the box shape stays reproducible. You can replace the placeholder contents with your own baseline configs.

### Why is there both `make doctor` and `make render`?

`make render` shows the intended model. `make doctor` checks whether the intended model and the runnable repo state still agree.

### Why is there both `workspace/dependencies.yaml` and `workspace/runtime.yaml`?

`workspace/dependencies.yaml` describes the runtime categories the box exposes. `workspace/runtime.yaml` declares the interior graph the new internal manager actually operates on: repos, installed skills, services, logs, and checks.

### Why ship a vendored skill packager?

So default skill packaging works from this repo alone. You do not need a sibling checkout just to build bundled `.skill` files.

### Should `default-skills/*.skill` live in the repo?

In this starter, yes. They represent the packaged default bundles the box can ship with. Other ad hoc `.skill` outputs should stay local.

### Can I add real repos under `repos/`?

Yes. That folder exists to mimic a narrower slice of your wider local repo workspace, and `workspace/runtime.yaml` is where you should start declaring them.

### Is `.env-manager/` the same thing as `../.env-manager`?

No. The outer `../.env-manager` launches boxes from outside. The in-repo `.env-manager/` manages the inside of this box.

## About Contributions

> *About Contributions:* Please don't take this the wrong way, but I do not accept outside contributions for any of my projects. I simply don't have the mental bandwidth to review anything, and it's my name on the thing, so I'm responsible for any problems it causes; thus, the risk-reward is highly asymmetric from my perspective. I'd also have to worry about other "stakeholders," which seems unwise for tools I mostly make for myself for free. Feel free to submit issues, and even PRs if you want to illustrate a proposed fix, but know I won't merge them directly. Instead, I'll have Claude or Codex review submissions via `gh` and independently decide whether and how to address them. Bug reports in particular are welcome. Sorry if this offends, but I want to avoid wasted time and hurt feelings. I understand this isn't in sync with the prevailing open-source ethos that seeks community contributions, but it's the only way I can move at this velocity and keep my sanity.

## License

No license file is included yet.
