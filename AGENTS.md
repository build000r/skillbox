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
- Run tests: `python3 -m unittest discover -s tests`
- Coverage: `make python-cov-xml`
- Build image: `make build`
- Start/stop shell: `make up`, `make shell`, `make down`
- Optional surfaces: `make up-surfaces`
- Runtime services: `make runtime-up CLIENT=<id> PROFILE=<name>`, `make runtime-down CLIENT=<id> PROFILE=<name>`, `make runtime-status`
- Box lifecycle: `make box-up BOX=<id>`, `make box-down BOX=<id>`, `make box-status`, `make box-list`, `make box-ssh BOX=<id>`
- Release/upgrade scripts: `install.sh`, `scripts/06-upgrade-release.sh`, `scripts/07-build-and-push-binary.sh`; verify arguments before use.
- CI: `.github/workflows/ci.yml` runs `python3 scripts/04-reconcile.py render` and `python3 -m unittest discover -s tests` on push/PR.
- Unknown / verify first: no repo-level lint command was found.

## Important Paths

- `workspace/runtime.yaml` declares repos, artifacts, skills, services, logs, checks, profiles, and client overlays.
- `workspace/sandbox.yaml`, `workspace/dependencies.yaml`, and `workspace/persistence.yaml` feed outer validation.
- `.env.example` documents supported env vars. `.env` and `.env.box` are local
  and ignored.
- `.env-manager/runtime_manager/` contains the Python runtime manager modules.
- `scripts/lib/runtime_model.py` builds the shared runtime model.
- `tests/` contains `unittest` coverage, including `tests/distribution/`.
- Runtime/log/generated state: `.skillbox-state/`, `logs/`, `invocations/`, `workspace/clients/`, `workspace/skill-repos/`, `workspace/.focus.json`, `workspace/boxes.json`, `sand/`, `builds/`.
- Generated agent context: `home/.claude/CLAUDE.md`, `home/.codex/AGENTS.md`.

## Testing Expectations

Run focused `python3 -m unittest ...` tests for touched modules, then broaden to
`python3 -m unittest discover -s tests` when practical. Use `make doctor` for outer drift and `make dev-sanity` for internal runtime validation.

Slow/side-effecting commands: `make build`, `make up`, `make runtime-sync`,
`make runtime-up`, `make box-up`, `make box-down`, and `install.sh` can build
containers, clone/download artifacts, start services, or touch infrastructure.

## Coding Notes

- Python is standard-library first; PyYAML is optional but required for YAML
  commands.
- Tests are `unittest` style and often import scripts by path with mocks around subprocess, Docker, network, and filesystem side effects.
- Keep CLI/MCP output structured and compact. Many handlers return JSON payloads
  with `ok`, `steps`, `checks`, `next_actions`, or structured error objects.
- Runtime commands should respect `--client`, repeatable `--profile`, and repeatable `--service`/`--task` scoping where applicable.
- Preserve user/local state. This repo commonly has dirty generated state and
  local secrets; do not clean ignored directories as part of code edits.

## Safety

- Do not commit secrets from `.env`, `.env.box`, `workspace/secrets/`, or local
  client overlays.
- Treat `make box-down`, `scripts/box.py down`, droplet destroy paths, Tailscale removal, and upgrade rollback paths as destructive; use dry-run or confirmation where supported.
- Do not run commands that download, clone, provision, or destroy unless the
  task requires that side effect.
- Avoid editing generated/runtime state unless the bug is specifically in that
  state contract.

<!-- br-agent-instructions-v1 -->

---

## Beads Workflow Integration

This project uses [beads_rust](https://github.com/Dicklesworthstone/beads_rust) (`br`/`bd`) for issue tracking. Issues are stored in `.beads/` and tracked in git.

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
