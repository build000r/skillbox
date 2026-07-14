# Operations

This page contains content moved from `README.md` during the README front-door split.

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
- reports unmanaged listeners through the port sentinel and can reap
  dev-server signatures when `SKILLBOX_PORT_SENTINEL=enforce`
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
- wildcard remote access is an explicit exposure exception: it requires opting in with `SKILLBOX_SWIMMERS_PUBLISH_HOST=0.0.0.0` (the helper exports this as `SWIMMERS_BIND`) and should not be treated as clean `tailnet_only` posture
- remote box helpers only promote loopback defaults to public bind when `SKILLBOX_SWIMMERS_EXPOSE=1` is set
- non-loopback publishing is blocked unless `SKILLBOX_SWIMMERS_AUTH_MODE=token` and `SKILLBOX_SWIMMERS_AUTH_TOKEN` are set
- remote box status prints the canonical phone/browser URL as `Open this on phone: http://<tailnet-ip>:3210/` and separately reports public SSH, Tailnet ping, MagicDNS resolution, and port reachability

Remote operator example:

```bash
# on the client skillbox
cat >> .env <<'EOF'
# Explicit exposure exception; not a clean tailnet_only bind.
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

Generated context also points agents at the agent operations brain, which is
the read-first command surface for understanding what this box can do and what
work is most actionable right now:

```bash
python3 .env-manager/manage.py capabilities --format json
python3 .env-manager/manage.py next --format json
```

This means the agent does not need to be told any of this manually. Every repo,
service, task, or skill you add to `runtime.yaml` or a client overlay
automatically appears in the agent context the next time `sync` or `context`
runs.

Both files are gitignored because they are generated state that varies by
environment and client selection.

### Agent operations brain

The brain commands turn the runtime graph, command registry, Beads/BV state,
SBP visibility, MCP parity, and recent evidence into a compact agent-native
API. They are designed to be safe defaults for a new agent session:

```bash
python3 .env-manager/manage.py capabilities --format json
python3 .env-manager/manage.py next --format json --limit 5
python3 .env-manager/manage.py graph --algorithm critical-path --format json
python3 .env-manager/manage.py explain brain.next --format json
python3 .env-manager/manage.py search "mcp parity" --format json
python3 .env-manager/manage.py snap replay tests/goldens/agent_ops_snapshot.json --format json
```

For the complete generated command contract, see `docs/API_REFERENCE.md`;
regenerate it with `python3 .env-manager/manage.py registry-docs --write`.

`capabilities`, `next`, `graph`, `explain`, and `search` are read-only.
`snap replay` and `snap diff` are read-only fixture operations. `snap create`
prints a redacted snapshot by default and writes under `.skillbox-state/` only
when `--write` is passed.

Use `--no-adapters` when you need deterministic local output without invoking
optional `br`, `bv`, `sbp`, or NTM probes. The MCP mirrors are
`skillbox_capabilities`, `skillbox_next`, `skillbox_graph`,
`skillbox_explain`, `skillbox_search`, and `skillbox_snap`, with the same
read-only/default-write behavior.

Focused validation for this surface:

```bash
python3 -m unittest tests.test_agent_ops_adapters tests.test_agent_ops_command_registry tests.test_agent_ops_graph tests.test_agent_ops_graph_algorithms tests.test_agent_ops_graph_engine tests.test_agent_ops_decisions tests.test_agent_ops_search tests.test_agent_ops_snapshots tests.test_agent_ops_golden_outputs tests.test_cli_units
```

Broaden to `python3 -m unittest discover -s tests`, then `make render`,
`make doctor`, and `make dev-sanity` before claiming production readiness. In
operator-migrated checkouts, current environmental blockers are expected to
surface as structured checks rather than silent drift: broken global skill or
MCP config symlinks report `broken_symlink`, and stale managed skill installs
can report missing shared support files until `runtime-sync` repairs them.
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
| `operator_teardown` | Full teardown: drain, remove from Tailnet, destroy droplet — **gated** (dry-run + clean repos) |
| `operator_box_exec` | Run a command on a remote box over Tailscale SSH — **gated** (read-only allowlist runs free; mutating/unknown commands need a per-command `dry_run=true` preview) |
| `operator_compose_up` | Build and start local containers |
| `operator_compose_down` | Stop all local containers — **gated** (dry-run + clean repos) |
| `operator_doctor` | Run outer validation checks |
| `operator_render` | Print the resolved sandbox model |

### Network Posture

Managed boxes default to `tailnet_only`: public SSH is a temporary bootstrap
aperture through `enroll`; after Tailscale enrollment succeeds, `box.py` locks
host SSH to Tailnet access and updates the DigitalOcean firewall so inbound
public SSH is closed. `posture-proof` verifies the box-level result with
`public_ssh_probe`, `tailnet_probe`, `cloud_firewall_rules`, and `violations`;
service bind exposure is verified by the runtime exposure lint. See
[docs/tailnet-only-lifecycle.md](tailnet-only-lifecycle.md) for the full
lifecycle, break-glass recovery paths, and proof-field mapping.

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

### operator_box_exec command gate

`operator_box_exec` runs arbitrary shell over Tailscale SSH, so it has its own
gate enforced **server-side** in `scripts/operator_mcp_server.py` (works for
every MCP client, not just Claude Code):

- **Read-only allowlist** runs unconditionally — no friction. The allowlist is
  short and conservative, matched on the leading token(s):
  `cat df du free head hostname id journalctl ls nproc ps pwd stat tail uname
  uptime wc whoami`, plus `docker {ps,logs,images,inspect,stats,version,top}`,
  `git {status,log,diff,show,branch,remote,rev-parse}`, and
  `systemctl {status,is-active,is-enabled,list-units,show}`. A command that
  contains shell chaining/redirection (`; | & > \` $( ${`, newlines), an
  env-var/path prefix, or that `cat`/`tail`s a secret-looking path does **not**
  get the fast path.
- **Everything else** (mutating verbs, unknown commands, chained commands)
  requires a fresh `dry_run=true` preview of the **identical** command. The
  preview returns exactly what would run and stamps a marker bound to
  `box_id + sha256(normalized command)`, so a marker for command A cannot
  authorize command B. Whitespace runs are normalized before hashing.
- **Every** invocation emits an `operator.box_exec` audit event (box id,
  command hash, redacted command, verdict, reason). Secrets are redacted before
  they reach the journal.

The PreToolUse hook (`guard-destructive-op.sh`) also matches `operator_box_exec`
as a backup: it lets previews through and blocks only unambiguously
catastrophic patterns (`rm -rf /`, `mkfs`, fork bombs, raw-device writes);
the server gate is authoritative.

> Note: `posture-proof` and `box status` probes shell out through `box.py`'s
> own SSH helpers, **not** through `operator_box_exec`, so this gate adds no
> friction to fleet posture/health automation.
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
| `make e2e-smoke` | Runs the opt-in read-only e2e smoke: render, doctor, runtime-render, sync dry-run, compose config, and loopback stub probes. Use `FORMAT=json` for machine output and `STRICT=1` to fail on doctor red checks. |
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
| `make box-status` | Health-check a remote box, including the phone URL, public SSH probe, Tailnet ping, MagicDNS, swimmers port reachability, and posture violations |
| `make box-list` | List all boxes from inventory |
| `make box-ssh` | SSH into a remote box |
| `make box-profiles` | List available box profiles |

### Scripts

| Script | Purpose | Example |
|---|---|---|
| `scripts/01-bootstrap-do.sh` | Bootstrap a fresh Ubuntu or DigitalOcean host | `sudo ./scripts/01-bootstrap-do.sh` |
| `scripts/02-install-tailscale.sh` | Join the tailnet and harden SSH. Standalone default keeps public SSH for recovery; managed `box up` passes `TAILNET_ONLY_SSH=true` for `tailnet_only` boxes. | `sudo TAILSCALE_AUTHKEY="tskey-..." TAILNET_ONLY_SSH=true ./scripts/02-install-tailscale.sh` |
| `scripts/04-reconcile.py render` | Print the resolved sandbox model | `python3 scripts/04-reconcile.py render --with-compose` |
| `scripts/04-reconcile.py doctor` | Run drift and readiness checks | `python3 scripts/04-reconcile.py doctor` |
| `scripts/05-swimmers.sh` | Manage the workspace-local swimmers install and process lifecycle | `./scripts/05-swimmers.sh status` |
| `scripts/operator_mcp_server.py` | Operator MCP server for fleet and container lifecycle | Runs via `.mcp.json` as `skillbox-operator` |
| `scripts/guard-destructive-op.sh` | PreToolUse hook gating destructive operator tools | Called automatically by Claude Code hooks |
| `.env-manager/manage.py context` | Generate CLAUDE.md and AGENTS.md from the resolved runtime graph | `python3 .env-manager/manage.py context --client personal` |
| `.env-manager/manage.py focus` | Activate a client with live state and enriched context | `python3 .env-manager/manage.py focus personal --format json` |
| `.env-manager/manage.py stewardship-report` | Build a client-scoped operator evidence packet with risks, proof, and not-assessed hardening gaps | `python3 .env-manager/manage.py stewardship-report personal --format md --write` |
| `.env-manager/manage.py parity-report` | Compare a client runtime graph against its production-stack parity contract | `python3 .env-manager/manage.py parity-report personal --format json` |
| `.env-manager/manage.py capabilities` | Return the machine-readable command registry, risks, examples, and MCP mirrors | `python3 .env-manager/manage.py capabilities --format json` |
| `.env-manager/manage.py next` | Rank explainable next actions from runtime, Beads/BV, SBP, MCP, and evidence signals | `python3 .env-manager/manage.py next --format json` |
| `.env-manager/manage.py graph` | Inspect the agent operations graph and run graph algorithms such as critical path, cycles, blast radius, and min-unblock | `python3 .env-manager/manage.py graph --algorithm critical-path --format json` |
| `.env-manager/manage.py explain` | Explain a command, graph node, Bead, skill, check, service, or MCP tool with evidence and next actions | `python3 .env-manager/manage.py explain brain.next --format json` |
| `.env-manager/manage.py search` | Search commands, graph nodes, docs, Beads, and evidence with grouped hits | `python3 .env-manager/manage.py search "skill sync" --format json` |
| `.env-manager/manage.py snap` | Create, diff, or replay redacted runtime snapshots; writes only on `snap create --write` | `python3 .env-manager/manage.py snap replay tests/goldens/agent_ops_snapshot.json --format json` |
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

Approved worker targets and excluded production boxes are operator policy, not
public defaults. Declare them in private box/client overlays before using this
lane.

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
## MCP Integration

Skillbox exposes two MCP servers for different contexts:

### Inside the box (agent tools)

`.env-manager/mcp_server.py` runs inside the workspace container and gives
agents tools to manage their own environment:

| Tool | Purpose |
|---|---|
| `skillbox_capabilities` | Machine-readable command registry, risk metadata, examples, and MCP mirrors |
| `skillbox_next` | Ranked next actions from runtime, graph, Beads/BV, SBP, NTM, and evidence signals |
| `skillbox_graph` | Typed agent operations graph plus algorithms such as cycles, critical path, blast radius, and min-unblock |
| `skillbox_explain` | Explanation packet for a graph node, command, Bead, skill, check, service, or MCP tool |
| `skillbox_search` | Grouped search across registry commands, graph nodes, selected docs, Beads, and evidence |
| `skillbox_snap` | Create, diff, or replay redacted snapshots; writes only when requested |
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
fleet lifecycle tools. See the [Fleet Management](operations.md#fleet-management) section.

## Clipboard bootstrap

Skillbox owns seamless paste plus OSC52 copy for operator Mac + Ghostty,
SSH/mosh remotes, nested tmux, and Conference1 direct WSL. Source bundle:
`scripts/clipboard/`. Design contract: `docs/clipboard-bootstrap.md`.

One-command bootstrap:

```bash
# Local operator Mac
scripts/clipboard-bootstrap --profile local

# Remote d3 portfolio devbox (plan)
scripts/clipboard-bootstrap --profile d3 --dry-run

# Apply on remote host
scripts/clipboard-bootstrap --profile d3 --apply-remote

# Generic target
scripts/clipboard-bootstrap --profile generic --target user@host --dry-run
```

Usage after install:

- Text/image paste: copy, focus the existing `d2`/`d3` pane, press `Cmd+V` or
  `Ctrl+V`; the router never chooses a host or sends Enter
- Text copy: `printf 'hello\n' | clipcopy` or tmux copy-mode `y` / Enter / mouse drag
- Linux `pbcopy` shim on remotes delegates to `clipcopy`
- Recovery only: `clipimg-put d|s|j|c` explicitly uploads and replaces the Mac
  clipboard with a remote path
- Truth surface: `clipboard-paste status|doctor|explain --profile d3`
- Reversal: `scripts/clipboard-bootstrap uninstall` or `rollback`
- Conference1: prefer direct `worker@conference1-wsl`; `conference1-ssh` Windows
  wrapper is OSC52-hostile fallback only

Proof and regression (two documented modes; see
[clipboard-bootstrap.md](clipboard-bootstrap.md#closeout-gates-and-proof-commands)):

```bash
# CI / source smoke — any Linux checkout; live paths recorded as SKIP with reasons
scripts/clipboard-closeout.sh
python3 -m unittest tests.test_clipboard_bootstrap tests.test_clipboard_closeout -v

# Operator / live rollout proof — exercises real SSH/tmux/nested-tmux/image
# paths; skipped core paths (d3, current-host migration, Ghostty, mosh) FAIL
# the run. Full PASS requires the operator Mac.
scripts/clipboard-closeout.sh --live
```

Durable per-run artifacts (JSON verdict + raw per-gate logs) land in
`~/.local/state/skillbox/clipboard-closeout/<stamp>-<mode>/`. Remote profiles
require the `~/.ssh/config` Host blocks documented in
[clipboard-bootstrap.md](clipboard-bootstrap.md#prerequisites).

### New-host clipboard adoption

Clipboard bootstrap is an **explicit manual/agent-run step**. It is not wired
into `install.sh`, `scripts/box.py`, or the env-manager lifecycle, and that is
deliberate: host adoption surfaces are security-sensitive, clipboard
integration is operator-optional, and remote writes should stay behind a
conscious `--apply-remote` invocation rather than ride along with enrollment.

Adopt a new host dry-run-first:

```bash
# 1. Plan (default for remote profiles; performs no remote writes)
scripts/clipboard-bootstrap --profile d3

# 2. Apply once the plan looks right (the only form that writes remotely)
scripts/clipboard-bootstrap --profile d3 --apply-remote

# Hosts without a named profile
scripts/clipboard-bootstrap --profile generic --target user@host                 # plan
scripts/clipboard-bootstrap --profile generic --target user@host --apply-remote  # apply
```

Validation from a fresh checkout (no SSH access or remote writes needed):

```bash
scripts/clipboard-bootstrap --profile d3
# expected: the per-step plan plus "note: remote writes require --apply-remote", exit 0
python3 -m unittest tests.test_clipboard_bootstrap -v
```
