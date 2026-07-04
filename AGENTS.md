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
- CI: `.github/workflows/ci.yml` runs Ruff, ShellCheck, compose config validation, `python3 scripts/04-reconcile.py render`, and the Python unittest matrix on push/PR.
- Python lint: `python3 -m ruff check .`
- Shell lint: `shellcheck --severity=warning scripts/*.sh install.sh`

## Important Paths

- `workspace/runtime.yaml` declares repos, artifacts, skills, services, logs, checks, profiles, and client overlays.
- `workspace/sandbox.yaml`, `workspace/dependencies.yaml`, and `workspace/persistence.yaml` feed outer validation.
- `docs/ARCHITECTURE.md` is the maintainer-grade system map for layers,
  manifests, runtime modules, data flow, state layout, and extension seams.
- `.env.example` documents supported env vars. `.env` and `.env.box` are local
  and ignored.
- `.env-manager/runtime_manager/` contains the Python runtime manager modules.
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

## Network Posture

Managed boxes default to `tailnet_only`: public SSH is a temporary bootstrap
aperture through `enroll`; after Tailscale enrollment succeeds, `box.py` locks
host SSH to Tailnet access and updates the DigitalOcean firewall so inbound
public SSH is closed. `posture-proof` verifies the box-level result with
`public_ssh_probe`, `tailnet_probe`, `cloud_firewall_rules`, and `violations`;
service bind exposure is verified by the runtime exposure lint. Do not bind
services to `0.0.0.0` on tailnet-only boxes — use loopback or Tailnet IP. See
`docs/tailnet-only-lifecycle.md` for recovery and exposure rules.

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

<!-- br-agent-instructions-v1 -->

---

## Beads Workflow Integration

This project uses [beads_rust](https://github.com/example/beads_rust) (`br`) for issue tracking. Issues are stored in `.beads/` and tracked in git.

### Essential Commands

```bash
# View ready issues (open, unblocked, not deferred)
br ready

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

<!-- bv-agent-instructions-v2 -->

---

## Beads Workflow Integration

This project uses [beads_rust](https://github.com/example/beads_rust) (`br`) for issue tracking and [beads_viewer](https://github.com/example/beads_viewer) (`bv`) for graph-aware triage. Issues are stored in `.beads/` and tracked in git.

### Using bv as an AI sidecar

bv is a graph-aware triage engine for Beads projects (.beads/beads.jsonl). Instead of parsing JSONL or hallucinating graph traversal, use robot flags for deterministic, dependency-aware outputs with precomputed metrics (PageRank, betweenness, critical path, cycles, HITS, eigenvector, k-core).

**Scope boundary:** bv handles *what to work on* (triage, priority, planning). `br` handles creating, modifying, and closing beads.

**CRITICAL: Use ONLY --robot-* flags. Bare bv launches an interactive TUI that blocks your session.**

#### The Workflow: Start With Triage

**`bv --robot-triage` is your single entry point.** It returns everything you need in one call:
- `quick_ref`: at-a-glance counts + top 3 picks
- `recommendations`: ranked actionable items with scores, reasons, unblock info
- `quick_wins`: low-effort high-impact items
- `blockers_to_clear`: items that unblock the most downstream work
- `project_health`: status/type/priority distributions, graph metrics
- `commands`: copy-paste shell commands for next steps

```bash
bv --robot-triage        # THE MEGA-COMMAND: start here
bv --robot-next          # Minimal: just the single top pick + claim command

# Token-optimized output (TOON) for lower LLM context usage:
bv --robot-triage --format toon
```

Before claiming, verify current state with `br show <id> --json` or `br ready --json`. `recommendations` can include graph-important blocked or assigned work; only `quick_ref.top_picks` and non-empty `claim_command` fields represent claimable work.

#### Other bv Commands

| Command | Returns |
|---------|---------|
| `--robot-plan` | Parallel execution tracks with unblocks lists |
| `--robot-priority` | Priority misalignment detection with confidence |
| `--robot-insights` | Full metrics: PageRank, betweenness, HITS, eigenvector, critical path, cycles, k-core |
| `--robot-alerts` | Stale issues, blocking cascades, priority mismatches |
| `--robot-suggest` | Hygiene: duplicates, missing deps, label suggestions, cycle breaks |
| `--robot-diff --diff-since <ref>` | Changes since ref: new/closed/modified issues |
| `--robot-graph [--graph-format=json\|dot\|mermaid]` | Dependency graph export |

#### Scoping & Filtering

```bash
bv --robot-plan --label backend              # Scope to label's subgraph
bv --robot-insights --as-of HEAD~30          # Historical point-in-time
bv --recipe actionable --robot-plan          # Pre-filter: ready to work (no blockers)
bv --recipe high-impact --robot-triage       # Pre-filter: top PageRank scores
```

### br Commands for Issue Management

```bash
br ready              # Show issues ready to work (no blockers)
br list --status=open # All open issues
br show <id>          # Full issue details with dependencies
br create --title="..." --type=task --priority=2
br update <id> --status=in_progress
br close <id> --reason="Completed"
br close <id1> <id2>  # Close multiple issues at once
br sync --flush-only  # Export DB to JSONL
```

### Workflow Pattern

1. **Triage**: Run `bv --robot-triage` to find the highest-impact actionable work
2. **Claim**: Use `br update <id> --status=in_progress`
3. **Work**: Implement the task
4. **Complete**: Use `br close <id>`
5. **Sync**: Always run `br sync --flush-only` at session end

### Key Concepts

- **Dependencies**: Issues can block other issues. `br ready` shows only unblocked work.
- **Priority**: P0=critical, P1=high, P2=medium, P3=low, P4=backlog (use numbers 0-4, not words)
- **Types**: task, bug, feature, epic, chore, docs, question
- **Blocking**: `br dep add <issue> <depends-on>` to add dependencies

### Session Protocol

```bash
git status              # Check what changed
git add <files>         # Stage code changes
br sync --flush-only    # Export beads changes to JSONL
git commit -m "..."     # Commit everything
git push                # Push to remote
```

<!-- end-bv-agent-instructions -->
