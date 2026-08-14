---
name: box-fleet-operator
description: Operate the skillbox fleet from outside the box with scripts/box.py — provision, inspect, exec on, and tear down remote boxes safely without the deprecated skillbox-operator MCP server. Use for "provision a box", "check the fleet", "run a command on box X", "tear down a box", "bring the local stack down", or any operator_* MCP tool that is no longer available.
---

# Box Fleet Operator

Operate the fleet from the **operator machine** (outside the box) using the
robot CLI. Every operation is a `python3 scripts/box.py <verb> --format json`
call from the repo root.

This skill replaces the deprecated **skillbox-operator MCP server**. If you were
reaching for `operator_boxes`, `operator_provision`, `operator_box_exec`, or any
other `operator_*` tool, use the CLI equivalent in the table below. The CLI
carries the same gates, in-process, so nothing is weakened by dropping the MCP
server.

Not to be confused with `skills/skillbox-operator`, which drives
`python3 .env-manager/manage.py` **inside** a box. This skill is the outside
view: droplets, Tailscale, inventory, remote exec.

## The Rule

1. **Look first.** `list` / `status` / `profiles` are read-only and cost nothing.
2. **Dry-run every mutation.** `--dry-run` previews *and* stamps the marker that
   authorizes the real run. There is no way to skip this legitimately.
3. **Confirm with the user before anything destructive.** `down` destroys a
   droplet. `compose-down` stops the local stack. Ask, in plain words, and wait.
4. **Act once.** Re-run the *identical* command without `--dry-run`.
5. **Verify.** `status <box-id> --format json`.

Never "helpfully" chain a real teardown onto a dry-run in the same turn.

## Tool Map (MCP → CLI)

| Deprecated MCP tool | Use this instead |
| --- | --- |
| `operator_profiles` | `python3 scripts/box.py profiles --format json` |
| `operator_boxes` | `python3 scripts/box.py list --format json` |
| `operator_box_status` | `python3 scripts/box.py status [<box-id>] --format json` |
| `operator_provision` | `python3 scripts/box.py up <box-id> --profile dev-small --dry-run --format json` |
| `operator_teardown` | `python3 scripts/box.py down <box-id> --dry-run --format json` |
| `operator_box_exec` | `python3 scripts/box.py exec <box-id> --format json -- <command>` |
| `operator_compose_down` | `python3 scripts/box.py compose-down --dry-run --format json` |
| `operator_compose_up` | `python3 scripts/box.py compose-up [--no-build] [--surfaces] --format json` |
| `operator_doctor` | `python3 scripts/04-reconcile.py doctor --format json` |
| `operator_render` | `python3 scripts/04-reconcile.py render [--with-compose] --format json` |

The live map is machine-readable:
`python3 scripts/box.py capabilities --format json` → `mcp_equivalents` and
`mcp_status`.

## Gates

Three independent gates protect the mutating verbs (`up`, `down`, `exec`,
`compose-down`, `upgrade`, `import`, `register`, `unregister`,
`inventory-rebuild`). They are enforced **inside box.py**, so they apply whether
you call it from Bash, a script, or a hook-less environment.

### 1. Clean tree

A real mutation refuses to run while the repo has uncommitted changes:

```
error_type: dirty_tree_refused
```

Commit or stash first. A dry-run does not fix this — the check runs before the
marker check on purpose.

### 2. Dry-run marker

`--dry-run` stamps a marker file under
`.skillbox-state/dryrun-markers/`. Properties you must internalize:

- **Bound to the exact command.** The key is derived from the box id **and** the
  hash of the command string. A marker minted for `docker ps` cannot authorize
  `rm -rf /`.
- **TTL 600 seconds.** Preview, confirm, act — promptly.
- **One marker, one real run.** A successful real run clears it. Running the
  same mutation twice means dry-running twice.
- **Shared store.** The marker format is byte-identical to the one the old MCP
  server used, so a preview taken through either surface authorizes the other.

Missing or stale marker:

```
error_type: dryrun_marker_required
```

`SKILLBOX_CLI_MUTATION_GATE=skip` exists for hermetic tests and break-glass
recovery only. Do not set it to get past a refusal; every skip prints a warning
to stderr and defeats the purpose of the gate.

### 3. Destructive command guard (DCG) — `exec` only

`exec` runs the pinned external `dcg` binary against the command string
immediately before the ssh. This is **authoritative** on both real-run paths —
the read-only allowlist fast path *and* the marker-authorized mutating path — so
neither an allowlist hit nor a valid marker is a bypass.

It **fails closed**. A deny, a missing binary, a spawn failure, a timeout,
malformed JSON, an incompatible schema or version, and an unrecognized decision
all block. There is no "no verdict" outcome.

```
error_type: dcg_denied      # the guard evaluated the command and said no
error_type: dcg_unavailable # the guard could not be trusted; nothing ran
```

On `--dry-run`, the guard result is **advisory** — it is attached to the payload
under `dcg` rather than failing the preview, because a preview executes nothing.
Read it: it tells you whether the real run will be blocked before you spend a
confirmation round-trip on it.

## exec: the two-tier policy

`box.py exec` classifies the command before anything else:

- **read-only** — an allowlisted inspection command (`docker ps`, `systemctl
  status`, `cat`, `ls`, `df`, log reads, `manage.py status`). Runs immediately,
  no marker friction. Still DCG-guarded.
- **mutating or unrecognized** — needs the clean tree + a fresh preview of the
  identical command. "Unrecognized" is treated as mutating, deliberately.

```bash
# read-only: one call
python3 scripts/box.py exec my-box --format json -- docker ps

# mutating: preview, confirm with the user, then act
python3 scripts/box.py exec my-box --dry-run --format json -- \
  'cd ~/skillbox && docker compose restart workspace'
# ... user confirms ...
python3 scripts/box.py exec my-box --format json -- \
  'cd ~/skillbox && docker compose restart workspace'
```

The command after `--` is shlex-joined into a single string. Keep it identical
between the preview and the real run, character for character, or the marker
will not match.

`box.py ssh` is for **interactive terminals only** (`make box-ssh BOX=<id>` for
a TTY). It is not gated the way `exec` is because it does not take a command;
do not use it as a way around `exec`.

## Workflow: provision a box

```bash
python3 scripts/box.py profiles --format json          # pick a size
python3 scripts/box.py list --format json              # check for an id conflict
python3 scripts/box.py up my-box --profile dev-small --dry-run --format json
```

Inspect `credential_status` in the dry-run output. Missing credentials go in the
**operator secret file** — `${SKILLBOX_STATE_ROOT}/operator/.env.box`, default
`./.skillbox-state/operator/.env.box` — and require
`SKILLBOX_DO_TOKEN`, `SKILLBOX_DO_SSH_KEY_ID`, `SKILLBOX_TS_AUTHKEY`.

Never put these in the repo root `.env`: in-container agents can read it.

Then, after the user confirms (this spends money):

```bash
python3 scripts/box.py up my-box --profile dev-small --format json
python3 scripts/box.py status my-box --format json
```

## Workflow: tear down a box

**Destroys infrastructure. Confirm with the user first, every time.**

```bash
python3 scripts/box.py down my-box --dry-run --format json
# ... user explicitly confirms ...
python3 scripts/box.py down my-box --yes --format json     # or --confirm my-box
```

The real teardown requires `--yes` or `--confirm <box-id>` *in addition to* the
marker. `--dry-run` needs no confirmation flag.

## Workflow: local stack

```bash
python3 scripts/box.py compose-up --dry-run --format json  # optional preview
python3 scripts/box.py compose-up --format json            # build + up -d
python3 scripts/box.py compose-up --no-build --surfaces --format json

python3 scripts/box.py compose-down --dry-run --format json
python3 scripts/box.py compose-down --format json          # after confirmation
```

**`compose-up` is the one mutating verb with no marker gate, and that is
deliberate.** It is constructive — it starts containers and destroys nothing —
and its inverse, `compose-down`, *is* gated. Requiring a clean tree to start
your dev environment would refuse the normal working state and train you to
reach for the skip variable, which is worse than no gate. Its `--dry-run` is a
real preview: it prints the compose commands and starts nothing.

Read `steps[]` in the response. `up` is the headline step; an optional
`up-surfaces` failure is reported in `partial_failures` without failing the
call, so a green exit does not by itself mean the api/web surfaces came up.

`make up` remains the ungated text-mode equivalent.

## Workflow: validate the repo

```bash
python3 scripts/04-reconcile.py doctor --format json     # drift, wiring, sync
python3 scripts/04-reconcile.py render --format json     # resolved model
```

Run `doctor` after cloning, after config changes, and before blaming the fleet
for something the local repo caused.

## Output contract

- With `--format json` (or `--json`), stdout is parseable JSON only. Diagnostics
  and alias notices go to stderr.
- Refusals are structured: `ok: false`, `error_type`, `recoverable`,
  `next_actions`. Follow `next_actions` rather than improvising.
- `exec` refusals carry `executed: false` — when you see one, nothing ran on the
  box.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 1 | runtime, environment, or user input error (includes gate refusals) |
| 2 | usage error (bad flags/arguments) |
| 3 | needs input |
| 4 | drift |

## Discovery

```bash
python3 scripts/box.py capabilities --format json   # verbs, gates, mcp_status
python3 scripts/box.py robot-docs guide             # prose orientation
python3 scripts/box.py --robot-triage               # "what should I run next"
python3 scripts/box.py <verb> --help
```

## Safety

1. Read-only before mutating. Always.
2. Dry-run before every real mutation. The marker is not a formality.
3. Confirm with the user before `down` and `compose-down`. State plainly what
   will be destroyed.
4. Never set `SKILLBOX_CLI_MUTATION_GATE=skip` to get past a refusal.
5. Never weaken or work around the DCG verdict on `exec`. If the guard is
   unavailable, fix the guard; do not ssh manually to do the same thing.
6. Operator credentials live in the operator secret file, never in the repo root.
7. A gate refusal is information, not an obstacle. Read `next_actions`.
