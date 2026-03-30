# skillbox

> A thin, self-hosted Tailnet dev box for AI-assisted coding on a Dockerized droplet.

![runtime](https://img.shields.io/badge/runtime-Docker-2496ED?style=flat-square&logo=docker&logoColor=white)
![access](https://img.shields.io/badge/access-Tailscale-242424?style=flat-square&logo=tailscale&logoColor=white)
![shape](https://img.shields.io/badge/shape-thin%20starter-6E7781?style=flat-square)
![doctor](https://img.shields.io/badge/doctor-manifest%20checks-2ea44f?style=flat-square)

```bash
cp .env.example .env
make doctor
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
- pin and package default skills locally
- validate manifest/runtime drift with `make doctor`

### Why Use `skillbox`?

| Need | `skillbox` answer |
|---|---|
| Private access without public SSH exposure | Tailscale host access plus host hardening scripts |
| A workspace that feels like a narrowed local setup | One bind-mounted `/workspace` with `repos/`, `skills/`, `logs/`, and home mounts |
| Reproducible default skills | `03-skill-sync.sh` packages from a pinned manifest and vendored local packager |
| Confidence that docs/config/runtime still match | `04-reconcile.py` powers `make render` and `make doctor` |
| Minimal surface area | No multi-tenant control plane, no hosted dependency, no hidden sibling repo requirement for packaging |

## Quick Example

This is the shortest useful local run:

```bash
cp .env.example .env
make render
make doctor
make build
make up
make up-surfaces
./scripts/03-skill-sync.sh
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/v1/sandbox
make shell
```

What that gives you:

- a validated box model
- a running workspace container
- optional API and web inspection surfaces
- packaged default `.skill` bundles under `default-skills/`

## Design Philosophy

### 1. Thin beats magical

This repo intentionally stops well short of Coder or Daytona. It focuses on one operator-controlled box and a small set of explicit scripts.

### 2. Host SSH, container work

SSH lands on the host. Docker Compose runs the workspace and optional surfaces. The container is where your day-to-day work should feel familiar.

### 3. Declarative enough to check, not so abstract it disappears

`workspace/sandbox.yaml`, `workspace/dependencies.yaml`, and the skill manifests describe the intended box. `make doctor` exists so that description can be checked against reality.

### 4. Portable skill packaging matters

The default skill packaging chain is vendored locally. A fresh clone does not need a sibling `../opensource` checkout just to package default skills.

### 5. Local-first operator ergonomics

The repo includes enough surfaces to inspect and validate the shape, but not so much that it becomes a platform you have to operate before you can work.

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
+-------------------------------+
| manifest + packaging layer    |
|-------------------------------|
| workspace/*.yaml              |
| 03-skill-sync.sh              |
| 04-reconcile.py               |
| scripts/package_skill.py      |
+-------------------------------+
```

## Troubleshooting

### `make doctor` fails on Compose validation

Check that Docker is installed and `docker compose config --format json` works on the host.

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

### Default skills look stale

Re-run:

```bash
./scripts/03-skill-sync.sh
make doctor
```

## Limitations

- This is not a hosted control plane or a multi-user workspace platform.
- There is no release installer, package manager distribution, or cloud provisioning flow yet.
- The API and web surfaces are inspection stubs, not a full UI.
- Repo cloning, secrets management, and per-project bootstrap are still your responsibility.
- There is no license file in this repo yet. Add one before publishing it as open source.

## FAQ

### Is SSH supposed to go to the host or the container?

The host. The container is the workspace runtime, not the SSH target.

### Why keep `home/.claude` and `home/.codex` in the repo?

So the box shape stays reproducible. You can replace the placeholder contents with your own baseline configs.

### Why is there both `make doctor` and `make render`?

`make render` shows the intended model. `make doctor` checks whether the intended model and the runnable repo state still agree.

### Why ship a vendored skill packager?

So default skill packaging works from this repo alone. You do not need a sibling checkout just to build bundled `.skill` files.

### Should `default-skills/*.skill` live in the repo?

In this starter, yes. They represent the packaged default bundles the box can ship with. Other ad hoc `.skill` outputs should stay local.

### Can I add real repos under `repos/`?

Yes. That folder exists to mimic a narrower slice of your wider local repo workspace.

## About Contributions

> *About Contributions:* Please don't take this the wrong way, but I do not accept outside contributions for any of my projects. I simply don't have the mental bandwidth to review anything, and it's my name on the thing, so I'm responsible for any problems it causes; thus, the risk-reward is highly asymmetric from my perspective. I'd also have to worry about other "stakeholders," which seems unwise for tools I mostly make for myself for free. Feel free to submit issues, and even PRs if you want to illustrate a proposed fix, but know I won't merge them directly. Instead, I'll have Claude or Codex review submissions via `gh` and independently decide whether and how to address them. Bug reports in particular are welcome. Sorry if this offends, but I want to avoid wasted time and hurt feelings. I understand this isn't in sync with the prevailing open-source ethos that seeks community contributions, but it's the only way I can move at this velocity and keep my sanity.

## License

No license file is included yet.
