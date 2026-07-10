# Skills

This page contains content moved from `README.md` during the README front-door split.

## Skillbox Forge

Skillbox Forge is the operator loop for turning real agent-session signal into
reviewed skill updates. It is passive by default: hooks collect evidence, status
summarizes the signal store, and proposals stay on `forge/<skill>` branches
until you explicitly accept or reject them.

Bootstrap the scoring hooks once:

```bash
python3 .env-manager/manage.py forge init --format json
# optional fallback for missed session hooks
python3 .env-manager/manage.py forge init --with-cron --format json
```

`forge init` is idempotent. It adds a Claude Code `SessionEnd` hook, patches the
installed `codex-tmux` wrapper when present, and can add a cron fallback. The
hook target is `scripts/score-session.sh`; it exits 0 when `skill-issue` is not
installed or no new transcripts are available, so session teardown is not
blocked by scoring.

After normal Claude/Codex work accumulates signal in
`~/.claude/skill-review-history.jsonl`, inspect it:

```bash
python3 .env-manager/manage.py forge status
python3 .env-manager/manage.py forge status --skill ask-cascade --format json
```

When a skill has enough scored sessions, create a reviewable proposal:

```bash
python3 .env-manager/manage.py forge propose ask-cascade --dry-run
python3 .env-manager/manage.py forge propose ask-cascade --min-sessions 5
```

`forge propose` requires a clean skill repo, sufficient signal, and no existing
`forge/<skill>` branch. It writes a minimal `SKILL.md` mutation on that branch
and logs the proposal to `~/.claude/forge-proposals.jsonl`. Review the branch
diff in the skill repo before deciding:

```bash
git -C workspace/skill-repos/<repo> diff main..forge/ask-cascade
python3 .env-manager/manage.py forge accept ask-cascade
# or
python3 .env-manager/manage.py forge reject ask-cascade --reason "too broad"
```

Accept fast-forward merges the proposal, deletes the forge branch, and logs to
`~/.claude/forge-decisions.jsonl`. Reject deletes the branch and records the
reason. After accepting, run the normal skill installation path so agent homes
pick up the reviewed skill change:

```bash
make runtime-sync
make dev-sanity
```

`make doctor` surfaces Forge trust checks without mutating state:
`SKILL_FORGE_HOOK_MISSING` when hooks are absent, `SKILL_FORGE_STALE` when
declining scored signal has no proposal, `SKILL_FORGE_PENDING` for open
`forge/*` branches, and `SKILL_FORGE_UNSCORED` when recent transcripts have not
been scored. Dirty repos, existing proposal branches, missing skills, and
fast-forward failures are intentional stop conditions; inspect and resolve the
skill repo before rerunning the Forge command.
### Skill repo config

`workspace/skill-repos.yaml` declares where skills come from:

```yaml
version: 2

skill_repos:
  - repo: build000r/skills
    ref: main
    pick: [ask-cascade, audit-plans, build-vs-clone, commit, crap, describe, divide-and-conquer, domain-planner, domain-reviewer, domain-scaffolder, mutate, oss-doc-audit, reproduce, skill-issue]

  - path: ../skills
    pick: [dev-sanity, skillbox-operator]
```

Each entry is either a GitHub repo (cloned into `workspace/skill-repos/`) or a
local path (referenced directly). `sync` clones or fetches repos, filtered-copies
skill directories into `~/.claude/skills/` and `~/.codex/skills/`, and writes a
lock file with resolved commit SHAs. Private writing skills and Cass-backed
memory skills are intentionally outside the core pack; Cass-backed memory skills
live in `workspace/skill-repos-memory.yaml` and are installed only when the
`memory` profile is active.

`skill_repos` also supports distributor-backed sources for reviewed
multi-client skill delivery. A config can declare a top-level `distributors`
section and then select skills from a distributor entry:

```yaml
version: 3

distributors:
  - id: acme-skills
    url: https://skills.acme.dev/api/v1
    client_id: client-42
    auth:
      method: api-key
      key_env: ACME_DISTRIBUTOR_KEY
    verification:
      public_key: "ed25519:..."

skill_repos:
  - distributor: acme-skills
    pick: [deploy, codebase-audit]
    pin:
      deploy: 7
```

Distributor sync fetches a signed per-client manifest, verifies signed
`.skillbundle.tar.gz` bundles, installs the selected skills through the same
filtered-copy path as repo and local-path sources, and records distributor
metadata in the generated lockfile. This is a sync-time delivery channel only;
installed skill content remains local and usable offline after sync.

The local product loop is also available for dogfooding reviewed skill
releases without a hosted control plane:

```bash
python3 .env-manager/manage.py distribution-publish ./path/to/skill \
  --version 1 \
  --manifest-path ./dist/manifest.json \
  --artifact-root ./dist/artifacts \
  --signing-key file:./dist/signing-key.pem \
  --distributor-id local-skills \
  --client-id client-42 \
  --target box \
  --capability deploy \
  --changelog "Initial release"

python3 .env-manager/manage.py distribution-preview \
  --manifest-path ./dist/manifest.json \
  --public-key "ed25519:..." \
  --distributor-id local-skills \
  --state-root ./.skillbox-state \
  --pick deploy \
  --format json

python3 .env-manager/manage.py sync --dry-run
python3 .env-manager/manage.py sync

python3 .env-manager/manage.py distribution-rollback \
  --manifest-path ./dist/manifest.json \
  --public-key "ed25519:..." \
  --distributor-id local-skills \
  --skill deploy \
  --version 1 \
  --state-root ./.skillbox-state \
  --install-target ~/.claude/skills \
  --lockfile ./workspace/skill-repos.lock.json \
  --reason "bad update"
```

Manifests can use `schema_version: 2` with per-version `artifacts[]`.
Preview is read-only: it verifies the signed manifest, resolves client pins and
manifest floors to a concrete artifact, reports cache/signature state, and
blocks missing selected artifacts before sync. Sync consumes the same selected
artifact metadata, so a client pin below the recommendation downloads and
installs the pinned artifact rather than the recommended one. Rollback uses the
verified bundle cache and records `pinned_by=rollback` in the lockfile.

Client overlays declare their own `skill-repos.yaml` under
`${SKILLBOX_CLIENTS_HOST_ROOT:-./workspace/clients}/<client>/skill-repos.yaml`.
### Effective skill visibility

`manage.py skills` is the conflict-aware view of what an agent can use for a
given cwd, client, and profile:

```bash
python3 .env-manager/manage.py skills --client personal --profile local-all --cwd "$PWD"
```

It layers declared default skills, matched client skills, project-local
`.claude/skills` or `.codex/skills`, and the operator's global
`~/.claude/skills` plus `~/.codex/skills`. The output shows one effective
winner per skill, counts lower-precedence shadowed sources, and flags broken
global symlinks, broken project-local links, undeclared global extras, skills
installed outside their declared scope, and skills that are expected for the
current cwd but are not available yet.

Use `--issues-only` for a compact agent/operator check, and `--show-sources`
when you intentionally want the larger inventory of source-tree skills that are
not currently synced. The `sbp candidates` wrapper is the short exploratory
path for that larger inventory when a clean policy check is not enough and the
agent needs to consider every linkable source skill.

Use `skill-audit` when the question is broader than the current cwd. It scans
the configured downstream repo roots from `skill-scope.yaml`, reports repo-local
missing skills and scope violations per repo, and summarizes global drift once
so agents do not repeat the same global warning for every checkout.

```bash
python3 .env-manager/manage.py skill-audit --cwd "$PWD"
python3 .env-manager/manage.py skill-audit --scan-root ~/repos --limit 20
```

Use `mcp-audit` when the question is whether the same repo exposes the right
MCP servers to both agent runtimes. It reads Claude Code project config from
`.mcp.json`, Codex project config from `.codex/config.toml`, and reports
missing, extra, disabled, invalid, and Claude-only/Codex-only MCP entries.
A config that is a dangling symlink (for example after a host migration) is
reported as `broken_symlink` with its target and a repair next-action instead
of being treated as merely absent.

```bash
python3 .env-manager/manage.py mcp-audit --cwd "$PWD"
sbp mcp
```

An operator config root may also define `skill-scope.yaml` or
`skills-scope.yaml` beside `clients/`. Those files declare repo-only or
global-allowed skill rules; `manage.py skills` reports `scope_violations` when
a skill is installed outside its declared availability. The policy can also
define named `project_categories` so rules can say `categories: [frontend]`
instead of repeating the same repo path list in every rule.

Repos can also carry a durable local override file at
`.skillbox/skill-overrides.yaml`. It is for repo-specific skill visibility
decisions that should survive `sbp recalibrate`: `pin_on` keeps a source-backed
skill visible in this repo, `pin_off` keeps it absent, `defaults` records repo
defaults managed by `skill default`, and `opt_out_global` suppresses
non-floor global defaults for this repo. Effective visibility is resolved in
this order: dispatcher floor policy first, then the repo override file, then
global defaults from `skill-scope.yaml`. The floor currently includes the
dispatcher/control-plane skills, so repo overrides cannot disable floor skills.

The same view is available to agents as the read-only `skillbox_skills` MCP
tool. Run it before adding, moving, or globally installing skills so the agent
can keep global utilities global and project/category skills local to the repos
where they belong.

Use the singular `skill` command when you want to apply that policy:

```bash
python3 .env-manager/manage.py skill plan mcp-server-design --cwd ~/repos/opensource/skillbox
python3 .env-manager/manage.py skill add mcp-server-design --cwd ~/repos/opensource/skillbox
python3 .env-manager/manage.py skill activate mcp-server-design --cwd ~/repos/opensource/skillbox
python3 .env-manager/manage.py skill why wiki --cwd "$PWD" --format json
python3 .env-manager/manage.py skill on wiki --cwd "$PWD" --dry-run
python3 .env-manager/manage.py skill off wiki --cwd "$PWD" --dry-run
python3 .env-manager/manage.py skill heal wiki --cwd "$PWD" --dry-run
python3 .env-manager/manage.py skill default on wiki --repo --cwd "$PWD" --dry-run
python3 .env-manager/manage.py skill add ui --to category --category frontend
python3 .env-manager/manage.py skill sync --cwd "$PWD" --dry-run
python3 .env-manager/manage.py skill prune --cwd "$PWD" --from project --dry-run
python3 .env-manager/manage.py skill prune --from global --dry-run
python3 .env-manager/manage.py skill lint --cwd "$PWD"
python3 .env-manager/manage.py skill-audit --cwd "$PWD"
python3 .env-manager/manage.py overlay activate marketing --cwd "$PWD"
```

`skill add` links the source skill into the selected global or repo-local
Claude/Codex skill roots. `--to auto` follows `skill-scope.yaml`: global
allowlist skills go into `~/.claude/skills` and `~/.codex/skills`; scoped
skills go under the matching repo/category as `.claude/skills` and
`.codex/skills`. `skill activate` performs the same link operation and also
prints an activation packet containing the source `SKILL.md`, so the current
agent session can use the skill immediately while future Claude and Codex
sessions discover the filesystem links normally. `overlay activate <name>` is
the cwd-scoped hot path: it evaluates policy with that overlay active for this
invocation only, links only the policy-allowed set for the cwd, and does not
persist overlay state. Pass `--to global` only when you intentionally want
operator-wide links in `~/.claude/skills` and `~/.codex/skills`; use
`overlay on` for persistent overlay state. `overlay off <name>` unlinks
cwd-local overlay symlinks by default; pass `--scope global` or `--scope all`
for wider cleanup. `remove`, `move`, and `prune` require `--dry-run` first or
`--yes` to apply unlinking actions.

The durable override verbs are the repo-local path for visibility decisions:

- `sbp skill why <name>` explains the winning layer, absence reason, and exact
  next command without mutating state.
- `sbp skill on <name>` writes `pin_on`, links the source-backed skill into the
  current repo, and returns an activation packet.
- `sbp skill off <name>` writes `pin_off` and unlinks project installs.
- `sbp skill heal <name>` resolves a real source, writes `pin_on`, links it,
  and returns an activation packet.
- `sbp skill default on|off <name> --repo` edits this repo's
  `.skillbox/skill-overrides.yaml`; `--global` edits operator policy and must be
  dry-run first, then applied with `--yes`.

Security contract: repo overrides may widen visibility only inside the current
repo. They cannot escalate a local-only skill into the global layer, cannot
disable dispatcher floor skills such as `smart` and `sbp`, and cannot make a
missing or typoed source effective. The prune firewall is local-widen-only:
project prune skips `pin_on` skills, removes `pin_off` project links, never
turns a repo pin into a global permission, and never prunes the dispatcher
floor. Use `sbp skill lint --cwd "$PWD"` after hand-editing the override file.

The repo-owned `scripts/sbp` and `scripts/sbo` wrappers delegate to the same
lifecycle surface. Install them as `~/.local/bin/sbp` and `~/.local/bin/sbo`
with `make wrappers-install`, or pass `--install-wrappers` to `install.sh`
when provisioning a checkout.

```bash
make wrappers-install
sbp
sbp help
sbp skills --issues-only
sbp candidates --json
sbp recalibrate
sbp mcp
sbp recalibrate --fleet --limit 20
sbp skills audit --limit 20
sbp skill plan mcp-server-design
sbp skill add mcp-server-design
sbp skill activate mcp-server-design
sbp skill why wiki --json
sbp skill on wiki --dry-run
sbp skill off wiki --dry-run
sbp skill heal wiki --dry-run
sbp skill default on wiki --repo --dry-run
sbp skill lint
sbp overlay activate marketing
sbp skill add ui --to category --category frontend
sbp skill sync --dry-run
sbp mmdx review
sbp mmdx skill_review_realms/review --no-open
sbp launch ../api ../web --request "Audit auth drift" --dry-run
sbo mmdx review
```

`sbp` with no args is intentionally read-only: it prints a fast cwd-aware skill
home view and next commands, skipping the slower global scan. Skill and overlay
commands infer the active client from the downstream cwd unless
`SKILLBOX_CLIENT` is set; wrapper runtime service commands pass the downstream
cwd through for the same client inference. Direct `manage.py` runtime calls
still use the configured default unless you pass `--client` or `--cwd`.
Runtime mutation is explicit with
`sbp up`, `sbp down`, `sbp restart`, or `sbp skill ...`; use
`sbp skills --issues-only` for the full global/project drift check and
`sbp candidates` for the full cwd-local linkable source inventory before
bucketing candidates into definitely, maybe, and no. Use `sbp recalibrate` for
the cwd-local add/remove recommendation, and
`sbp skills audit` or `sbp recalibrate --fleet` for the cross-repo map.
`sbp recalibrate` prints the dry-run sync/prune commands for the current repo
and the Claude/Codex MCP parity check; the fleet form prints the narrow dry-run
commands to review before applying repo-local, global, or overlay cleanup.
`sbp mcp` runs only the read-only MCP audit. `sbp mmdx` is the short path for
downstream Mermaid/MMDX diagrams: it fuzzy-matches from the current repo and
opens the selected file through the Buildooor diagrams viewer, while
`--no-open` lists or resolves candidates without launching a browser.

`sbp launch` and its `sbp bulk` alias are Swimmers batch launchers for agents:
pass one or more directories plus `--request` (or `--request-file`) and they
POST to the running Swimmers `/v1/sessions/batch` API, creating one agent
session per directory. Use `--dry-run --json` first to inspect the exact
payload; set `--base-url` or `SWIMMERS_TUI_URL` for a non-default Swimmers API.

Agents can apply the same flow through the `skillbox_skill` and
`skillbox_overlay` MCP tools after a dry-run review. For diagrams, use
`skillbox_mmdx_open` with `open=false` to resolve/list candidates before
launching the viewer.
### Generated skill lockfiles

`make runtime-sync` writes `workspace/skill-repos.lock.json` for the shared
default skill set, and client overlays write their own lockfiles under
`${SKILLBOX_CLIENTS_HOST_ROOT:-./workspace/clients}/<client>/skill-repos.lock.json`.

Each lockfile records:

- the config file SHA (detects when the config changed since last sync)
- the resolved commit SHA per skill (what ref was installed)
- the installed tree SHA per skill (for drift detection)
- distributor manifest versions, bundle hashes, selected versions, and pin
  reasons for distributor-backed skills

These lockfiles are generated state and are gitignored, so running sync does
not turn normal local runtime reconciliation into noisy repo dirt.
