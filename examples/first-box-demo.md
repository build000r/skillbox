# First Box Demo: Zero to Focused Client

Captured on 2026-07-05 UTC from a working tree based on commit `e6f21b0`.

This walkthrough starts from a clean clone, creates a `demo-client` overlay from
the `git-repo-http-service-bootstrap` blueprint, starts a loopback-only HTTP
service, verifies it with `curl`, then removes the demo state and returns to a
doctor-green checkout.

Output is copied from a real run on this checkout. Normalizations:

- `<ROOT>` means the checkout root.
- `<PID>`, `<TAILNET-IP>`, and `<MAGICDNS>` are host-specific values.
- `<TS>` replaces web-server timestamps.
- Some long command output is shown as an excerpt and marked with `[... omitted ...]`.

Do not run this as a single `set -e` script: the first `make doctor` is a
pre-sync baseline and is expected to exit nonzero before `runtime-sync` creates
generated lock/state files.

## Clone

```bash
git clone https://github.com/build000r/skillbox.git skillbox
cd skillbox
git rev-parse --short HEAD
```

Captured output:

```text
Cloning into '<ROOT>'...
done.
e6f21b0
```

## Seed Operator Env

Start with the standard env example, then move it to the operator state root.
`make doctor` treats a repo-root `.env` as agent-visible secret exposure.

```bash
cp .env.example .env
mkdir -p .skillbox-state/operator
mv .env .skillbox-state/operator/.env
```

Captured output:

```text
# no output
```

## Render The Box Model

```bash
make render
```

Captured output excerpt:

```text
sandbox: skillbox
purpose: cloneable-tailnet-dev-box
runtime: tailnet-docker as sandbox
entrypoints: ssh, manual, api, swimmers, web

env defaults:
  SKILLBOX_NAME=skillbox
  SKILLBOX_STORAGE_PROVIDER=local
  SKILLBOX_STATE_ROOT=./.skillbox-state
  SKILLBOX_STORAGE_FILESYSTEM=
  SKILLBOX_STORAGE_REQUIRED=false
  SKILLBOX_STORAGE_MIN_FREE_GB=0
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
[... omitted ...]
  claude-home: <ROOT>/.skillbox-state/home/.claude -> /home/sandbox/.claude (persistent)
  codex-home: <ROOT>/.skillbox-state/home/.codex -> /home/sandbox/.codex (persistent)
  local-home: <ROOT>/.skillbox-state/home/.local -> /home/sandbox/.local (persistent)
  ntm-config: <ROOT>/.skillbox-state/home/.config/ntm -> /home/sandbox/.config/ntm (persistent)
  clients-root: <ROOT>/.skillbox-state/clients -> /workspace/workspace/clients (persistent)
  logs-root: <ROOT>/.skillbox-state/logs -> /workspace/logs (persistent)
  monoserver-root: <ROOT>/.skillbox-state/monoserver -> /monoserver (persistent)

skill sync:
  config: workspace/skill-repos.yaml
  lockfile: workspace/skill-repos.lock.json
  clone root: workspace/skill-repos
  declared skills: divide-and-conquer, lube, mmdx, project-status-mmdx, skill-issue, smart
  locked skills: (none)

runtime manager:
  script: .env-manager/manage.py
  manifest: workspace/runtime.yaml
  persistence: workspace/persistence.yaml
  clients: 0
  repos: 4
  skills: 2
  services: 11
  logs: 7
  checks: 18
```

## Baseline Doctor

```bash
make doctor
```

Expected first-run result: exit code `2`. The checkout has not synced generated
skill locks yet, so this is a useful baseline rather than the stopping point.

Captured output excerpt:

```text
PASS required-files: required files are present
     checked: 27
PASS expected-files-sources: expected-files CORE list and derivation sources resolve cleanly
     core: 6
     derived: 21
PASS expected-directories: expected workspace directories are present
PASS manifest-alignment: manifest files agree on runtime paths
PASS env-defaults: .env.example matches manifest defaults
WARN skill-repo-lock-state: workspace skill repo lockfile is missing
     expected_path: workspace/skill-repos.lock.json
     fix: make runtime-sync
WARN beads-state: Beads database is missing
     expected_path: .beads/beads.db
     fix: sbp beads init --cwd .
PASS reference-drift: no stale 00-skill-sync.sh references were found
PASS runtime-manager-model: internal runtime manager manifest resolved successfully
[... omitted ...]
PASS secrets-visible-in-workspace: no operator secret files are exposed inside workspace bind mounts
PASS skill-repo-sync-dry-run: manage.py sync --dry-run can resolve the configured default skill-repo-set
     preview: exists: <ROOT>, exists: <ROOT>/repos, skip: <ROOT>/.skillbox-state/home/.local/bin/swimmers (artifact source url missing), skip: <ROOT>/.skillbox-state/home/.local/bin/dcg (artifact source url missing)

summary: 13 passed, 2 warnings, 1 failed
make: *** [Makefile:106: doctor] Error 1
```

## Sync Runtime State

```bash
make runtime-sync
```

Captured output:

```text
exists: <ROOT>
exists: <ROOT>/repos
skip: <ROOT>/.skillbox-state/home/.local/bin/swimmers (artifact source url missing)
skip: <ROOT>/.skillbox-state/home/.local/bin/dcg (artifact source url missing)
skip: <ROOT>/.skillbox-state/home/.local/bin/apr (artifact source url missing)
skip: <ROOT>/.skillbox-state/home/.local/bin/ubs (artifact source url missing)
ensure-directory: <ROOT>/.skillbox-state/logs/runtime
ensure-directory: <ROOT>/.skillbox-state/logs/repos
skill-repo-cloned: build000r/skills
install-shared-skill-asset: _shared -> <ROOT>/.skillbox-state/home/.claude/skills/_shared
install-shared-skill-asset: _shared -> <ROOT>/.skillbox-state/home/.codex/skills/_shared
install-skill: divide-and-conquer -> <ROOT>/.skillbox-state/home/.claude/skills/divide-and-conquer
install-skill: divide-and-conquer -> <ROOT>/.skillbox-state/home/.codex/skills/divide-and-conquer
install-skill: lube -> <ROOT>/.skillbox-state/home/.claude/skills/lube
install-skill: lube -> <ROOT>/.skillbox-state/home/.codex/skills/lube
install-skill: mmdx -> <ROOT>/.skillbox-state/home/.claude/skills/mmdx
install-skill: mmdx -> <ROOT>/.skillbox-state/home/.codex/skills/mmdx
install-skill: project-status-mmdx -> <ROOT>/.skillbox-state/home/.claude/skills/project-status-mmdx
install-skill: project-status-mmdx -> <ROOT>/.skillbox-state/home/.codex/skills/project-status-mmdx
install-skill: skill-issue -> <ROOT>/.skillbox-state/home/.claude/skills/skill-issue
install-skill: skill-issue -> <ROOT>/.skillbox-state/home/.codex/skills/skill-issue
install-skill: smart -> <ROOT>/.skillbox-state/home/.claude/skills/smart
install-skill: smart -> <ROOT>/.skillbox-state/home/.codex/skills/smart
write-lockfile: <ROOT>/workspace/skill-repos.lock.json
render-dcg-config: <ROOT>/.dcg.toml (packs: core.git, core.filesystem)
write-context: home/.claude/CLAUDE.md
symlink-context: home/.codex/AGENTS.md -> ../.claude/CLAUDE.md
```

## Create A Demo Client

The blueprint clones a tiny public repository, runs a bootstrap command that
writes a hello-world page and health file, then starts `python3 -m http.server`
on loopback port `43117`.

```bash
BOOTSTRAP_COMMAND='python3 -c '"'"'from pathlib import Path; Path(".skillbox").mkdir(exist_ok=True); Path("index.html").write_text("hello from demo-client\n", encoding="utf-8"); Path("health").write_text("ok\n", encoding="utf-8"); Path(".skillbox/bootstrap.ok").write_text("ok\n", encoding="utf-8")'"'"''
SERVICE_COMMAND='bash -lc '"'"'set -a; [ -f .skillbox-port.env ] && . ./.skillbox-port.env; set +a; python3 -m http.server "${PORT:-43117}" --bind "${HOST:-127.0.0.1}"'"'"''

python3 .env-manager/manage.py client-init demo-client \
  --blueprint git-repo-http-service-bootstrap \
  --set PRIMARY_REPO_URL=https://github.com/octocat/Hello-World.git \
  --set PRIMARY_REPO_BRANCH=master \
  --set SERVICE_PORT=43117 \
  --set BOOTSTRAP_COMMAND="$BOOTSTRAP_COMMAND" \
  --set SERVICE_COMMAND="$SERVICE_COMMAND"
```

Captured output:

```text
blueprint: git-repo-http-service-bootstrap
write-file: .skillbox-state/clients/demo-client/overlay.yaml
write-file: .skillbox-state/clients/demo-client/skill-repos.yaml
write-file: .skillbox-state/clients/demo-client/skills/.gitkeep
write-file: .skillbox-state/clients/_shared/skills/.gitkeep
write-file: .skillbox-state/clients/demo-client/plans/INDEX.md
write-file: .skillbox-state/clients/demo-client/plans/draft/.gitkeep
write-file: .skillbox-state/clients/demo-client/plans/released/.gitkeep
write-file: .skillbox-state/clients/demo-client/plans/sessions/.gitkeep
copy-skill-template: .skillbox-state/clients/_shared/skills/domain-planner
copy-skill-template: .skillbox-state/clients/_shared/skills/domain-reviewer
copy-skill-template: .skillbox-state/clients/_shared/skills/domain-scaffolder
copy-skill-template: .skillbox-state/clients/_shared/skills/divide-and-conquer
```

## Sync The Client

```bash
make runtime-sync CLIENT=demo-client
```

Captured output:

```text
exists: <ROOT>
exists: <ROOT>/repos
skip: <ROOT>/.skillbox-state/monoserver (sync mode external)
clone-if-missing: https://github.com/octocat/Hello-World.git -> <ROOT>/.skillbox-state/monoserver/app
skip: <ROOT>/.skillbox-state/home/.local/bin/swimmers (artifact source url missing)
skip: <ROOT>/.skillbox-state/home/.local/bin/dcg (artifact source url missing)
skip: <ROOT>/.skillbox-state/home/.local/bin/apr (artifact source url missing)
skip: <ROOT>/.skillbox-state/home/.local/bin/ubs (artifact source url missing)
render-port-contract: <ROOT>/.skillbox-state/monoserver/app/.skillbox-port.env (1 service(s))
exists: <ROOT>/.skillbox-state/logs/runtime
exists: <ROOT>/.skillbox-state/logs/repos
ensure-directory: <ROOT>/.skillbox-state/logs/clients/demo-client
ensure-directory: <ROOT>/.skillbox-state/logs/clients/demo-client/services
skill-repo-fetched: build000r/skills
install-shared-skill-asset: _shared -> <ROOT>/.skillbox-state/home/.claude/skills/_shared
install-shared-skill-asset: _shared -> <ROOT>/.skillbox-state/home/.codex/skills/_shared
install-skill: divide-and-conquer -> <ROOT>/.skillbox-state/home/.claude/skills/divide-and-conquer
install-skill: divide-and-conquer -> <ROOT>/.skillbox-state/home/.codex/skills/divide-and-conquer
install-skill: lube -> <ROOT>/.skillbox-state/home/.claude/skills/lube
install-skill: lube -> <ROOT>/.skillbox-state/home/.codex/skills/lube
install-skill: mmdx -> <ROOT>/.skillbox-state/home/.claude/skills/mmdx
install-skill: mmdx -> <ROOT>/.skillbox-state/home/.codex/skills/mmdx
install-skill: project-status-mmdx -> <ROOT>/.skillbox-state/home/.claude/skills/project-status-mmdx
install-skill: project-status-mmdx -> <ROOT>/.skillbox-state/home/.codex/skills/project-status-mmdx
install-skill: skill-issue -> <ROOT>/.skillbox-state/home/.claude/skills/skill-issue
install-skill: skill-issue -> <ROOT>/.skillbox-state/home/.codex/skills/skill-issue
install-skill: smart -> <ROOT>/.skillbox-state/home/.claude/skills/smart
install-skill: smart -> <ROOT>/.skillbox-state/home/.codex/skills/smart
lockfile-unchanged: <ROOT>/workspace/skill-repos.lock.json
install-skill: domain-planner -> <ROOT>/.skillbox-state/home/.claude/skills/domain-planner
install-skill: domain-planner -> <ROOT>/.skillbox-state/home/.codex/skills/domain-planner
install-skill: domain-reviewer -> <ROOT>/.skillbox-state/home/.claude/skills/domain-reviewer
install-skill: domain-reviewer -> <ROOT>/.skillbox-state/home/.codex/skills/domain-reviewer
install-skill: domain-scaffolder -> <ROOT>/.skillbox-state/home/.claude/skills/domain-scaffolder
install-skill: domain-scaffolder -> <ROOT>/.skillbox-state/home/.codex/skills/domain-scaffolder
install-skill: divide-and-conquer -> <ROOT>/.skillbox-state/home/.claude/skills/divide-and-conquer
install-skill: divide-and-conquer -> <ROOT>/.skillbox-state/home/.codex/skills/divide-and-conquer
write-lockfile: <ROOT>/.skillbox-state/clients/demo-client/skill-repos.lock.json
exists: <ROOT>/.dcg.toml
write-context: home/.claude/CLAUDE.md
exists: home/.codex/AGENTS.md -> ../.claude/CLAUDE.md
```

## Focus The Client

```bash
python3 .env-manager/manage.py focus demo-client --wait-seconds 10
```

Captured output:

```text
[ok] compose-override
[ok] sync
[ok] bootstrap
[ok] up
[ok] collect
[ok] skill-context
[ok] context
[ok] persist

  Client:    demo-client
  Repos:     4 present, 1 dirty
  Services:  2 running, 1 down
  Checks:    7/10 passing
  Pressure:  4 warning(s); run pressure-report before cleanup/build storms
```

## Check Status

```bash
python3 .env-manager/manage.py status --client demo-client
```

Captured output:

```text
Open this on phone: http://<TAILNET-IP>:3210/
MagicDNS: http://<MAGICDNS>:3210/
clients: demo-client
default client: (none)
active clients: demo-client
active profiles: core
repos:
  - skillbox-self: present, git main...origin/main, 2 dirty, 1 untracked
  - managed-repos: present
  - demo-client-root: present
  - app: present, git master...origin/master, 0 dirty, 4 untracked
artifacts:
  - swimmers-bin: missing (url)
  - dcg-bin: missing (url)
  - apr-bin: missing (url)
  - ubs-bin: missing (url)
env files:
skills:
  - default-skills: lock present, 6 skills, 10/12 targets healthy
  - demo-client-skills: lock present, 4 skills, 8/8 targets healthy
tasks:
  - app-bootstrap: ready
services:
  - internal-env-manager [covered]: declared (orchestration services are status-only)
  - pulse [covered]: running (pid <PID>), health path_exists
  - app-dev [covered]: running (pid <PID>), bootstrap app-bootstrap, health http -> http://127.0.0.1:43117 [loopback-only]
blocked services:
  - internal-env-manager
pressure/offload:
  - local: 14.75GiB free (9.58%, high)
  - target: worker-devbox state=unknown
  - rch: no-workers worker=no-workers
  - sbh: not-configured daemon=missing-daemon
  ! Local disk pressure is high; avoid expensive local build storms and inspect pressure-report first.
  ! SBH storage guard is not observing; cleanup remains manual review only.
  ! SBH latest Linux release asset has a known mismatch; keep the verified canary pin.
  ! Protected paths are hard vetoes; do not delete agent state or SSH material.
logs:
  - runtime: 5 files, 13.1KiB
  - repos: 0 files, 0.0B
  - demo-client: 3 files, 266.0B
  - demo-client-services: 3 files, 266.0B
checks:
  - workspace-root: ok
  - repos-root: ok
  - skills-root: ok
  - log-root: ok
  - monoserver-root: ok
  - runtime-manager: ok
  - dcg-binary: missing
  - apr-binary: missing
  - ubs-binary: missing
  - demo-client-root: ok
```

The `skillbox-self` dirty/untracked counts above came from the capture working
tree. On a clean checkout, expect those counts to be lower.

## Curl The Service

```bash
curl -fsS http://127.0.0.1:43117/
```

Captured output:

```text
hello from demo-client
```

## Read Logs

```bash
python3 .env-manager/manage.py logs --client demo-client --service app-dev --lines 20
```

Captured output:

```text
[app-dev] <ROOT>/.skillbox-state/logs/clients/demo-client/services/app-dev.log
127.0.0.1 - - [<TS>] "GET /health HTTP/1.1" 200 -
127.0.0.1 - - [<TS>] "GET /health HTTP/1.1" 200 -
127.0.0.1 - - [<TS>] "GET / HTTP/1.1" 200 -
127.0.0.1 - - [<TS>] "GET /health HTTP/1.1" 200 -
```

## Stop Runtime Services

Use unscoped client `down` so both the demo HTTP service and the pulse daemon
stop.

```bash
python3 .env-manager/manage.py down --client demo-client
```

Captured output:

```text
services:
  - app-dev: stopped (pid <PID>)
  - pulse: stopped (pid <PID>)
  - internal-env-manager: skipped (orchestration services are status-only)
```

## Cleanup

Remove the demo client overlay, its cloned app, generated focus file, and
client logs. Then rerun `runtime-sync` so shared default skill installs are
restored after the client overlay is removed.

```bash
rm -rf \
  .skillbox-state/clients/demo-client \
  .skillbox-state/logs/clients/demo-client \
  .skillbox-state/monoserver/app \
  workspace/.focus.json

make runtime-sync
make doctor
```

Captured cleanup output:

```text
# rm -rf produced no output
```

Captured `make runtime-sync` output:

```text
exists: <ROOT>
exists: <ROOT>/repos
skip: <ROOT>/.skillbox-state/home/.local/bin/swimmers (artifact source url missing)
skip: <ROOT>/.skillbox-state/home/.local/bin/dcg (artifact source url missing)
skip: <ROOT>/.skillbox-state/home/.local/bin/apr (artifact source url missing)
skip: <ROOT>/.skillbox-state/home/.local/bin/ubs (artifact source url missing)
exists: <ROOT>/.skillbox-state/logs/runtime
exists: <ROOT>/.skillbox-state/logs/repos
skill-repo-fetched: build000r/skills
install-shared-skill-asset: _shared -> <ROOT>/.skillbox-state/home/.claude/skills/_shared
install-shared-skill-asset: _shared -> <ROOT>/.skillbox-state/home/.codex/skills/_shared
install-skill: divide-and-conquer -> <ROOT>/.skillbox-state/home/.claude/skills/divide-and-conquer
install-skill: divide-and-conquer -> <ROOT>/.skillbox-state/home/.codex/skills/divide-and-conquer
install-skill: lube -> <ROOT>/.skillbox-state/home/.claude/skills/lube
install-skill: lube -> <ROOT>/.skillbox-state/home/.codex/skills/lube
install-skill: mmdx -> <ROOT>/.skillbox-state/home/.claude/skills/mmdx
install-skill: mmdx -> <ROOT>/.skillbox-state/home/.codex/skills/mmdx
install-skill: project-status-mmdx -> <ROOT>/.skillbox-state/home/.claude/skills/project-status-mmdx
install-skill: project-status-mmdx -> <ROOT>/.skillbox-state/home/.codex/skills/project-status-mmdx
install-skill: skill-issue -> <ROOT>/.skillbox-state/home/.claude/skills/skill-issue
install-skill: skill-issue -> <ROOT>/.skillbox-state/home/.codex/skills/skill-issue
install-skill: smart -> <ROOT>/.skillbox-state/home/.claude/skills/smart
install-skill: smart -> <ROOT>/.skillbox-state/home/.codex/skills/smart
lockfile-unchanged: <ROOT>/workspace/skill-repos.lock.json
exists: <ROOT>/.dcg.toml
write-context: home/.claude/CLAUDE.md
exists: home/.codex/AGENTS.md -> ../.claude/CLAUDE.md
```

Captured final `make doctor` output:

```text
PASS required-files: required files are present
     checked: 27
PASS expected-files-sources: expected-files CORE list and derivation sources resolve cleanly
     core: 6
     derived: 21
PASS expected-directories: expected workspace directories are present
PASS manifest-alignment: manifest files agree on runtime paths
PASS env-defaults: .env.example matches manifest defaults
PASS skill-repo-lock-state: workspace skill repo lockfile matches the declared picks
     skills: divide-and-conquer, lube, mmdx, project-status-mmdx, skill-issue, smart
WARN beads-state: Beads database is missing
     expected_path: .beads/beads.db
     fix: sbp beads init --cwd .
PASS reference-drift: no stale 00-skill-sync.sh references were found
PASS runtime-manager-model: internal runtime manager manifest resolved successfully
     manifest: workspace/runtime.yaml
     persistence_manifest: workspace/persistence.yaml
     clients: 0
     repos: 4
     skills: 2
     services: 11
     logs: 7
     checks: 18
     storage_bindings: 8
PASS runtime-manager-doctor: internal runtime manager doctor completed without failures
     warnings: 5
     warning_codes: skill-repo-install, SKILL_FORGE_HOOK_MISSING, SKILL_FORGE_UNSCORED, PORT_REGISTRY_WARNING, PORT_REGISTRY_WARNING
PASS compose-config: docker compose config resolved for default, surfaces, and swimmers overlay variants
PASS compose-workspace: workspace service matches manifest-derived env and mounts
PASS compose-surfaces: api/web services match manifest-derived env, profile, and ports
PASS compose-swimmers: workspace swimmers overlay matches manifest-derived env and port publishing
PASS secrets-visible-in-workspace: no operator secret files are exposed inside workspace bind mounts
PASS skill-repo-sync-dry-run: manage.py sync --dry-run can resolve the configured default skill-repo-set
     preview: exists: <ROOT>, exists: <ROOT>/repos, skip: <ROOT>/.skillbox-state/home/.local/bin/swimmers (artifact source url missing), skip: <ROOT>/.skillbox-state/home/.local/bin/dcg (artifact source url missing)

summary: 15 passed, 1 warnings, 0 failed
```

At this point `demo-client` is gone, no demo services are running, and doctor has
no failures.
