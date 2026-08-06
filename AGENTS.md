# AGENTS.md

Guide for coding agents. Keep changes scoped and verify facts locally before
extending this document.

## Project Shape

`skillbox` is a private, single-tenant Tailnet/Docker dev box for one operator
and their coding agents. Durable state defaults to `.skillbox-state/` and is
mounted into the workspace as agent homes, logs, clients, and optional
monoserver state.

Main entry points:
- `Makefile` wraps the common host/operator commands.
- `scripts/04-reconcile.py` validates and renders the outer repo model.
- `.env-manager/manage.py` re-exports `runtime_manager` and runs the runtime CLI.
- `.env-manager/runtime_manager/cli.py` defines runtime subcommands.
- `scripts/box.py` manages DigitalOcean/Tailscale box lifecycle.
- `scripts/operator_mcp_server.py` exposes operator lifecycle tools over MCP.
- `scripts/stub_api.py` and `scripts/stub_web.py` are optional local surfaces.

## Core Commands

- Bootstrap env: `make bootstrap-env` or `cp .env.example .env`
- Outer render/check: `make render`, `make doctor`
- Runtime render/sync/check: `make runtime-render`, `make runtime-sync`, `make dev-sanity`
- Agent ops brain: `python3 .env-manager/manage.py capabilities --format json`, then `python3 .env-manager/manage.py next --format json`
- Agent graph/search: `python3 .env-manager/manage.py graph --format json`, `python3 .env-manager/manage.py explain brain.next --format json`, `python3 .env-manager/manage.py search "<query>" --format json`
- Agent snapshots: `python3 .env-manager/manage.py snap replay tests/goldens/agent_ops_snapshot.json --format json`; `snap create --write` writes redacted local state under `.skillbox-state/`
- Agent brain latency proof: `python3 tests/perf/brain_proof.py --cycles 5` (standalone, outside default unittest discovery)
- Run tests: `python3 -m unittest discover -s tests`
- Coverage: `make python-cov-xml`
- Build image: `make build`
- Start/stop shell: `make up`, `make shell`, `make down`
- Optional surfaces: `make up-surfaces`
- Runtime services: `make runtime-up CLIENT=<id> PROFILE=<name>`, `make runtime-down CLIENT=<id> PROFILE=<name>`, `make runtime-status`
- Box lifecycle: `make box-up BOX=<id>`, `make box-down BOX=<id>`, `make box-status`, `make box-list`, `make box-ssh BOX=<id>`
- Release/upgrade scripts: `install.sh`, `scripts/06-upgrade-release.sh`, `scripts/07-build-and-push-binary.sh`; verify arguments before use.
- Clipboard bootstrap (explicit manual step; not run by `install.sh`/`box.py`): `scripts/clipboard-bootstrap --profile local|d3|sweet|jeremy|conference1 [--dry-run|--apply-remote]` — remote profiles print a plan by default and only write with `--apply-remote`. Canonical flow: "New-host clipboard adoption" in `docs/operations.md`; bundle in `scripts/clipboard/`; closeout `scripts/clipboard-closeout.sh`; design `docs/clipboard-bootstrap.md`.
- Canonical local CI gate: `make self-test` (or `./scripts/self-test.sh --rev <rev>`) runs Ruff, ShellCheck, compose config validation, `scripts/04-reconcile.py render`, and the pinned 3.11/3.12/3.13 unittest matrix with 3.12 coverage against an isolated checkout of an exact SHA, then writes a receipt under `.skillbox-state/self-test/receipts/`. `.githooks/pre-push` runs it and blocks the push on failure (`make install-hooks`).
- CI: `.github/workflows/ci.yml` runs the same lanes for `pull_request` and `workflow_dispatch` only — trusted-main pushes are gated locally, not on Actions. `.github/workflows/release.yml` is unchanged (`v*` tags + manual, OIDC keyless signing). See "Local CI gate" in `docs/operations.md`.
- Python lint: `python3 -m ruff check .`
- Shell lint: `shellcheck --severity=warning scripts/*.sh install.sh`

## Important Paths

- `workspace/runtime.yaml` declares repos, artifacts, skills, services, logs, checks, profiles, and client overlays.
- `workspace/sandbox.yaml`, `workspace/dependencies.yaml`, and `workspace/persistence.yaml` feed outer validation.
- `README.md` is the short front door. Moved long-form README content lives in
  `docs/runtime-graph.md`, `docs/clients.md`, `docs/skills.md`,
  `docs/operations.md`, `docs/clipboard-bootstrap.md`, `docs/troubleshooting.md`, and `docs/faq.md`.
- `docs/ARCHITECTURE.md` is the maintainer-grade system map for layers,
  manifests, runtime modules, data flow, state layout, and extension seams.
- `docs/amp/skillbox-project-orb-vision.md` is the accepted contract for the
  disposable Amp project Orb, its readiness and skill adapter, and its remote
  auth/deploy authority stops.
- `.env.example` documents supported env vars. `.env` and `.env.box` are local
  and ignored.
- `.env-manager/runtime_manager/` contains the Python runtime manager modules.
  `agent_search.py` indexes the README, AGENTS.md, and the focused docs pages.
- `scripts/lib/runtime_model.py` builds the shared runtime model.
- `tests/` contains `unittest` coverage, including `tests/distribution/`.
- `.skillbox/skill-overrides.yaml` is the repo-local durable skill visibility
  override file used by `sbp skill on/off/heal/default --repo`.
- Runtime/log/generated state: `.skillbox-state/`, `logs/`, `invocations/`, `workspace/clients/`, `workspace/skill-repos/`, `workspace/.focus.json`, `workspace/boxes.json`, `sand/`, `builds/`.
- Generated agent context: `home/.claude/CLAUDE.md`, `home/.codex/AGENTS.md`.

## Testing Expectations

Run focused `python3 -m unittest ...` tests for touched modules, then broaden to
`python3 -m unittest discover -s tests` when practical. For the agent ops brain,
use `python3 -m unittest tests.test_agent_ops_adapters tests.test_agent_ops_command_registry tests.test_agent_ops_graph tests.test_agent_ops_graph_algorithms tests.test_agent_ops_graph_engine tests.test_agent_ops_decisions tests.test_agent_ops_search tests.test_agent_ops_snapshots tests.test_agent_ops_golden_outputs tests.test_cli_units`.
Use `make doctor` for outer drift and `make dev-sanity` for internal runtime validation.

Slow/side-effecting commands: `make build`, `make up`, `make runtime-sync`,
`make runtime-up`, `make box-up`, `make box-down`, and `install.sh` can build
containers, clone/download artifacts, start services, or touch infrastructure.
`capabilities`, `next`, `graph`, `explain`, `search`, `snap replay`, and
`snap diff` are read-only; `snap create --write` is the only brain command that
writes local generated state.

## Skill Overrides

- Check live skill visibility with `sbp skills --issues-only --json`,
  `sbp candidates --json`, and `sbp skill why <name> --json` before changing
  links or policy.
- Effective skill precedence is: dispatcher floor policy > repo override
  `.skillbox/skill-overrides.yaml` > global defaults from `skill-scope.yaml`.
- Durable repo verbs are `sbp skill on <name>`, `sbp skill off <name>`,
  `sbp skill heal <name>`, and `sbp skill default on|off <name> --repo`.
  Use `--dry-run` first when available; use `sbp skill lint` after hand-editing
  `.skillbox/skill-overrides.yaml`.
- `sbp skill why` and `sbp skill lint` are read-only. `activate`, `add`,
  `move`, `remove`, `sync`, and `prune` manage links but are not durable repo
  override decisions unless paired with an override verb.
- The prune firewall is local-widen-only: project prune skips `pin_on` skills,
  removes `pin_off` project links, never grants global visibility, and never
  disables dispatcher floor skills such as `smart` or `sbp`.

## Background Task Polling

Do not hand-roll `while/for` loops with `sleep` and `grep` to poll for
background task completion. Use the Monitor tool to stream events from a
background process (each stdout line becomes a notification), or use `Bash` with
`run_in_background` and wait for the notification. For a polling pattern, use
Monitor with an until-loop: `until <check>; do sleep 2; done` — you get a
notification when the loop exits. Only use `sleep` in a poll loop when no
notification mechanism is available.

## Deferred Tools

Common tools (TaskCreate, TaskUpdate, WebSearch, WebFetch, Monitor) are
deferred and unusable until their schemas are fetched. If you expect to need
any of them, batch-load them with a single ToolSearch call in your first turn
(e.g. `select:TaskCreate,TaskUpdate,WebSearch,WebFetch,Monitor`) instead of
paying one ToolSearch round-trip per tool later.

## Network Posture

Managed boxes default to `tailnet_only`: public SSH is a temporary bootstrap
aperture through `enroll`; after Tailscale enrollment succeeds, `box.py` locks
host SSH to Tailnet access and updates the DigitalOcean firewall so inbound
public SSH is closed. `posture-proof` verifies the box-level result with
`public_ssh_probe`, `tailnet_probe`, `cloud_firewall_rules`, and `violations`;
service bind exposure is verified by the runtime exposure lint. Do not bind
services to `0.0.0.0` on tailnet-only boxes — use loopback or Tailnet IP. See
`docs/tailnet-only-lifecycle.md` for recovery and exposure rules.
For the Conference1 heavy-build box (tailnet Serve URLs, read-only status,
Swimmers remote Rust lane), use `sbp conference1`. Real host metadata lives in
the operator's private hosts registry (`SKILLBOX_CLIPBOARD_HOSTS`); the tracked
`scripts/clipboard/hosts.json` ships sanitized `.example` values.

## Coding Notes

- Python is standard-library first; PyYAML is optional but required for YAML
  commands.
- Tests are `unittest` style and often import scripts by path with mocks around subprocess, Docker, network, and filesystem side effects.
- Keep CLI/MCP output structured and compact. Many handlers return JSON payloads
  with `ok`, `steps`, `checks`, `next_actions`, or structured error objects.
- Runtime commands should respect `--client`, repeatable `--profile`, and repeatable `--service`/`--task` scoping where applicable.
- New agent-facing commands should be registered in
  `.env-manager/runtime_manager/command_registry.py`, exposed through both CLI
  and in-box MCP when useful, and covered by focused `tests/test_agent_ops_*`
  tests when they touch graph, search, decision, snapshot, or registry behavior.
- Preserve user/local state. This repo commonly has dirty generated state and
  local secrets; do not clean ignored directories as part of code edits.

## Safety

- Do not commit secrets from `.env`, `.env.box`, `workspace/secrets/`, or local
  client overlays.
- Skill overrides cannot be used for global escalation. Durable `on`/`off`/`heal`
  pins are repo-local; global defaults must go through
  `sbp skill default on|off <name> --global --dry-run` and apply with `--yes`.
  `off`/`default off` cannot disable dispatcher floor skills.
- Treat `make box-down`, `scripts/box.py down`, droplet destroy paths, Tailscale removal, and upgrade rollback paths as destructive; use dry-run or confirmation where supported.
- The `operator_box_exec` MCP tool is gated server-side
  (`scripts/operator_mcp_server.py`): a short read-only allowlist
  (status/journalctl/df/`docker ps`/`docker logs`/`git status`/`cat` of
  non-secret paths/etc.) runs unconditionally, but any mutating or unknown
  command — or anything with shell chaining/redirection — is rejected until you
  re-issue the IDENTICAL command with `dry_run=true` first. The preview stamps a
  marker bound to `box_id + sha256(normalized command)`, so a marker for one
  command never authorizes another. Every invocation is audited
  (`operator.box_exec`) with the command redacted. `posture-proof`/`box status`
  do NOT route through `operator_box_exec`, so the gate adds no friction there.
- Do not run commands that download, clone, provision, or destroy unless the
  task requires that side effect.
- Avoid editing generated/runtime state unless the bug is specifically in that
  state contract.

## Amp project-Orb contract (AO-005)

Hardened project-backed Orb lane for local repository work. The durable
Skillbox box remains a separate system; see
`docs/amp/skillbox-project-orb-vision.md`.

- **Setup:** run `.agents/setup`. It is bounded, offline, and idempotent. It
  checks fixed Orb disk headroom, compiles `.env-manager`/`scripts`/`tests`,
  runs `python3 -m unittest tests.test_agent_ops_adapters -q`, creates a
  private stable resume identity, and evaluates `.agents/orb-capabilities.json`.
- **Resume:** run `.agents/resume` on wake. It performs only bounded local
  command/disk, identity, and readiness checks. It does not repair, install,
  join networks, start services, or background work.
- **Readiness:** run `python3 scripts/orb/orb_readiness.py collect --context
  manual`. Optional `--output
  .skillbox-state/project-orb/hook-state/orb-readiness.json` writes a sanitized
  mode-0600 receipt. Capability states are `ready`, `configured`, `degraded`,
  `blocked`, and `forbidden`; this evaluator never uses network.
- **Status/logs:** setup and resume write private status, identity, readiness,
  and log files under `.skillbox-state/project-orb/hook-state` and
  `.skillbox-state/project-orb/hook-logs`. Override
  `AGENT_STATE_DIR`/`AGENT_LOG_DIR` in tests. Legacy tracked `.agents/state` and
  `.agents/logs` evidence is not runtime state. Receipts include only
  enumerated status fields and reason codes.
- **Typed failures:** `setup=10`, `dependency=20`, `capacity=30`, `auth=40`,
  `validation=50`. Every hook subprocess has a hard elapsed timeout.
- **Skills:** the existing SBP lifecycle projects one selected source to
  `.claude/skills`, `.codex/skills`, and project-local `.agents/skills`. There
  is no global Amp target or second source/policy/lock lifecycle.
- **Remote auth:** authenticated `sbpd` requires a project allowlist and exact
  project alias. `workspace_id` is optional unless a real token and verifier
  contract require it. The client mints short-lived Amp tokens in memory; no
  static-secret fallback is allowed.
- **External authority:** hosted SPAPS acceptance and production deploy/apply
  require real relying-party/operator receipts. Local fixtures, configured env
  names, and dry-run receipts do not satisfy those gates.
- **Do not:** start Docker/SPAPS/monoserver, load operator `.env` secrets,
  expose public listeners, or touch production box/infrastructure lifecycle
  from ordinary Orb setup/readiness.

<!-- br-agent-instructions-v1 -->

---

## Beads Workflow Integration

This project uses beads_rust (`br`/`bd`) for issue tracking. Issues are stored in `.beads/` and tracked in git.

### Essential Commands

```bash
# View ready issues (open, unblocked, not deferred)
br ready              # or: bd ready

# List and search
br list --status=open # All open issues
br show <id>          # Full issue details with dependencies
br search "keyword"   # Full-text search

# Create and update
br create --title="..." --description="..." --type=task --priority=2
br update <id> --status=in_progress
br close <id> --reason="Completed"
br close <id1> <id2>  # Close multiple issues at once

# Sync with git
br sync --flush-only  # Export DB to JSONL
br sync --status      # Check sync status
```

### Workflow Pattern

1. **Start**: Run `br ready` to find actionable work
2. **Claim**: Use `br update <id> --status=in_progress`
3. **Work**: Implement the task
4. **Complete**: Use `br close <id>`
5. **Sync**: Always run `br sync --flush-only` at session end

### Key Concepts

- **Dependencies**: Issues can block other issues. `br ready` shows only open, unblocked work.
- **Priority**: P0=critical, P1=high, P2=medium, P3=low, P4=backlog (use numbers 0-4, not words)
- **Types**: task, bug, feature, epic, chore, docs, question
- **Blocking**: `br dep add <issue> <depends-on>` to add dependencies

### Session Protocol

**Before ending any session, run this checklist:**

```bash
git status              # Check what changed
git add <files>         # Stage code changes
br sync --flush-only    # Export beads changes to JSONL
git commit -m "..."     # Commit everything
git push                # Push to remote
```

### Best Practices

- Check `br ready` at session start to find available work
- Update status as you work (in_progress → closed)
- Create new issues with `br create` when you discover tasks
- Use descriptive titles and set appropriate priority/type
- Always sync before ending session

<!-- end-br-agent-instructions -->
