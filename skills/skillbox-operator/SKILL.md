---
name: skillbox-operator
type: operator
description: Manage the skillbox runtime graph from inside the box. Onboard projects via blueprints, orchestrate services, diagnose and heal runtime issues, and maintain box health through manage.py. Activate when the user asks about box state, wants to set up a project, needs service management, or when something in the environment is broken.
---

# Skillbox Operator

## Default Marker

Start with a stable first progress message such as:

`Using skillbox-operator to assess box state before making changes.`

## The Rule

Every operation follows the same discipline: **assess, scope, dry-run, act, verify.**

1. **Assess** -- run `status` and `doctor` to understand current state before doing anything
2. **Scope** -- use `--client`, `--service`, `--profile` to target exactly what you intend to change
3. **Dry-run** -- run mutating commands with `--dry-run` first to preview what will happen
4. **Act** -- after confirming the dry-run output with the user, run without `--dry-run`
5. **Verify** -- run `doctor` or `status` after the operation to confirm success

Never skip steps. Never run an unscoped mutation. Never act without assessing first.

## Assess Before Operating

Before any operation, establish situational awareness:

```bash
# Current state: repos, services, skills, checks
python3 .env-manager/manage.py status --format json [--client <id>]

# Structural health: drift, missing paths, broken installs
python3 .env-manager/manage.py doctor --format json [--client <id>]
```

Parse the JSON output. Key signals:

- `status.services[].state` -- is the service running, stopped, or dead?
- `status.repos[].present` -- is the repo cloned?
- `status.tasks[].state` -- has the bootstrap task completed (`ready`) or is it `pending`?
- `status.checks[].ok` -- are filesystem checks passing?
- `doctor[].status` -- `pass`, `warn`, or `fail`
- `doctor[].code` -- identifies which check failed

Fix `fail` results from doctor before attempting other operations.

## Operations Reference

All commands: `python3 .env-manager/manage.py <command> [flags]`

### Inspect (safe, no side effects)

```bash
render  [--format json] [--client <id>]                # Full resolved runtime graph
status  [--format json] [--client <id>]                # Current runtime state
doctor  [--format json] [--client <id>]                # Health validation
logs    [--service <id>] [--lines N] [--client <id>]   # Service log tail (default: 40 lines)
client-diff <id> --target-dir <repo> [--profile <id>]  # Review candidate bundle vs current published payload
```

### Mutate (use --dry-run first, confirm with user)

```bash
sync      [--dry-run] [--client <id>]                          # Create dirs, clone repos, install skills
bootstrap [--dry-run] [--client <id>] [--task <id>]            # Run one-shot bootstrap tasks
up        [--dry-run] [--client <id>] [--service <id>]         # Sync + bootstrap + start services
down      [--dry-run] [--client <id>] [--service <id>]         # Stop services
restart   [--dry-run] [--client <id>] [--service <id>]         # Stop + sync + bootstrap + start
context   [--dry-run] [--client <id>]                          # Regenerate CLAUDE.md / AGENTS.md
```

### Macro (runs a full workflow)

```bash
onboard <id> [--blueprint <name>] [--set KEY=VALUE ...] [--dry-run] [--format json]  # Full onboard flow
```

### Scaffold (creates new files)

```bash
client-init --list-blueprints [--format json]                                  # List blueprints
client-init <id> --blueprint <name> --set KEY=VALUE [--set ...] [--dry-run]    # Scaffold client
```

### Promotion review

```bash
client-project <id> [--profile <id>] [--format json]                           # Build a single-client projection bundle
client-diff <id> --target-dir <repo> [--profile <id>] [--format json]          # Compare candidate bundle vs published payload
client-publish <id> --target-dir <repo> [--commit] [--profile <id>]            # Promote the reviewed bundle
```

## Structured JSON Output

All commands with `--format json` include a `next_actions` list suggesting what to run next. On error, the JSON includes a structured `error` object:

```json
{
  "error": {
    "type": "service_health_failure",
    "message": "Service app-dev failed to become healthy.",
    "recoverable": true,
    "recovery_hint": "Check service logs for the root cause, then restart."
  },
  "next_actions": ["logs --format json", "doctor --format json"]
}
```

Use `next_actions` to decide what to do after each step. Use `error.recovery_hint` when an operation fails.

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error (check `error` in JSON output) |
| 2 | Drift detected (doctor found failures, run sync to fix) |

## Workflow: Onboard a New Project

Use the `onboard` macro to scaffold, sync, bootstrap, start services, generate context, and verify in one step:

```bash
python3 .env-manager/manage.py onboard <client-id> --blueprint <blueprint> --set KEY=VALUE --format json
```

Preview first with `--dry-run`. The output includes a `steps` array with status for each phase (scaffold, sync, bootstrap, up, context, verify).

To choose a blueprint, list what is available:

```bash
python3 .env-manager/manage.py client-init --list-blueprints --format json
```

Built-in blueprints and their required `--set` variables:

| Blueprint | Use case | Required variables |
|-----------|----------|--------------------|
| `git-repo` | Clone a repo, set as cwd | `PRIMARY_REPO_URL` |
| `git-repo-http-service` | Clone + managed dev server | `PRIMARY_REPO_URL`, `SERVICE_COMMAND` |
| `git-repo-http-service-bootstrap` | Clone + install step + dev server | `PRIMARY_REPO_URL`, `BOOTSTRAP_COMMAND`, `SERVICE_COMMAND` |

For the hardened v1 release path, prefer `git-repo-http-service-bootstrap`.
That is the blessed onboarding path that pairs with `private-init`,
client-local planning roots under `skillbox-config/clients/<client>/plans/`,
and `client-publish --acceptance`.

All blueprints accept optional overrides for repo IDs, paths, ports, and healthcheck URLs. Run `--list-blueprints --format json` to see every variable, its default, and description.

For manual step-by-step control, use `client-init`, `sync`, `bootstrap`, `up`, and `context` individually.

## Workflow: Diagnose and Fix

When something seems broken or the user reports a problem:

**1. Doctor** -- find structural issues:

```bash
python3 .env-manager/manage.py doctor --format json [--client <id>]
```

**2. Status** -- find runtime state issues:

```bash
python3 .env-manager/manage.py status --format json [--client <id>]
```

**3. Logs** -- find application errors:

```bash
python3 .env-manager/manage.py logs --service <id> --lines 100
```

**4. Apply the right fix:**

| Symptom | Fix |
|---------|-----|
| Missing directories or repos | `sync [--client <id>]` |
| Service not running | `up --service <id> [--client <id>]` |
| Service unhealthy or crashed | Read logs, fix the underlying issue, `restart --service <id>` |
| Bootstrap task incomplete | `bootstrap --task <id> [--client <id>]` |
| Skill install missing or stale | `sync [--client <id>]` |
| CLAUDE.md out of date | `context [--client <id>]` |

**5. Verify** after fixing: `doctor --format json [--client <id>]`

## Workflow: Service Lifecycle

```bash
# Start services for a client
python3 .env-manager/manage.py up --client <id> [--service <id>]

# Check what is running
python3 .env-manager/manage.py status --client <id> --format json

# Read recent logs
python3 .env-manager/manage.py logs --service <id> --lines 100

# Restart after code changes
python3 .env-manager/manage.py restart --client <id> --service <id>

# Stop
python3 .env-manager/manage.py down --client <id> [--service <id>]
```

## Workflow: Box Lifecycle (DigitalOcean + Tailscale)

Create and destroy full infrastructure from the operator's machine using `scripts/box.py`.

```bash
# List available box profiles
python3 scripts/box.py profiles --format json

# Preview what would happen
python3 scripts/box.py up <client-id> --profile dev-small --dry-run --format json

# Create: droplet → bootstrap → tailscale → deploy → onboard → verify
python3 scripts/box.py up <client-id> --profile dev-small [--blueprint <name>] [--set KEY=VALUE] --format json

# Check health
python3 scripts/box.py status <client-id> --format json

# SSH in
python3 scripts/box.py ssh <client-id>

# Destroy: drain → remove from tailnet → delete droplet
python3 scripts/box.py down <client-id> --format json

# List all active boxes
python3 scripts/box.py list --format json
```

Required env vars in `.env` or `.env.box`: `SKILLBOX_DO_TOKEN`, `SKILLBOX_DO_SSH_KEY_ID`, `SKILLBOX_TS_AUTHKEY`.

Box profiles live in `workspace/box-profiles/*.yaml` and declare region, size, image, and ssh user.

## Safety

1. **Always `--dry-run` first** for sync, up, down, restart, bootstrap, client-init, onboard, and box up/down.
2. **Always scope** with `--client` and `--service` when targeting a specific project or service. Never run unscoped `down` or `restart` unless the user explicitly asks to stop everything.
3. **Confirm with the user** before `down` (stops running processes), `restart`, `box down` (destroys infrastructure), or any edit to runtime YAML or overlay files.
4. **Never edit `.env`** without explicit user approval -- it may contain secrets or host-specific values.
5. **Use `--format json`** for inspection commands when you need to parse output programmatically. Use text format when showing results directly to the user.
6. **Read logs before escalating errors** -- the answer is usually in the service output.
7. **Client IDs** must be lowercase alphanumeric with single hyphens: `my-project`, `acme-api`, `personal`.
