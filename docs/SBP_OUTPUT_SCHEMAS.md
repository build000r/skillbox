<!-- GENERATED FILE — do not hand-edit. -->
<!-- Regenerate: python3 scripts/gen_output_schemas.py --write (or REGEN_OUTPUT_SCHEMA_DOCS=1 python3 -m pytest tests/test_output_schema_docs.py). -->
<!-- Generated from: scripts/gen_output_schemas.py + tests/fixture_fleet.py goldens. -->

# sbp Output Schemas

Documented JSON shapes for every `sbp` output surface agents parse. For each surface: the producing function, a field-by-field table marking each field **CONTRACT** (depend on it) vs *info* (advisory; shape may evolve), and ONE example payload.

This page is **generated** from `scripts/gen_output_schemas.py`. The example payloads are produced live by calling the real payload functions against the deterministic `tests/fixture_fleet.py` estate (the same harness the golden tests use), so they cannot drift from the runtime. `tests/test_output_schema_docs.py` locks the committed file to the generator output.

## Reading the stability column

- **CONTRACT** — the field's presence and meaning are part of the agent-facing contract; parse and branch on it. The `summary` / `by_class` counter *sets* are add-only (new keys may appear; existing keys keep their meaning).
- *info* — advisory, human-facing, or best-effort (e.g. `reason`, `recommendations`, `next_actions`, `duration_s`). Useful to surface, but its exact shape/wording may evolve.

## Surfaces

- [`sbp capabilities`](#sbp-capabilities)
- [`sbp skills`](#sbp-skills)
- [`sbp candidates`](#sbp-candidates)
- [`sbp mcp`](#sbp-mcp)
- [`sbp recalibrate`](#sbp-recalibrate)
- [`sbp skill why`](#sbp-skill-why)
- [`sbp skill on`](#sbp-skill-on)
- [`sbp skill off`](#sbp-skill-off)
- [`sbp skill togglable`](#sbp-skill-togglable)
- [`sbp explain`](#sbp-explain)
- [`sbp doctor`](#sbp-doctor)
- [`fleet converge`](#fleet-converge)

---

## `sbp capabilities`

**Invocation:** `sbp capabilities --json`
**Produced by:** `scripts/sbp print_capabilities`

The wrapper discovery contract. Agents should start here to learn the stable command inventory, stdout/stderr rules, dry-run guidance, and the machine-readable `skill_verbs` decision map for choosing between recalibrate/activate/sync/prune/on/off/heal/why/togglable and maintenance verbs.

### Fields

| Field | Stability | Meaning |
|-------|-----------|---------|
| `agent_surfaces` | CONTRACT | Canonical discovery commands for agents. |
| `aliases` | CONTRACT | Sibling wrapper aliases for this entrypoint. |
| `commands` | CONTRACT | Agent-facing command inventory with safe_first_try examples. |
| `contract_version` | CONTRACT | Version tag for this wrapper capabilities contract. |
| `cwd` | CONTRACT | Invocation cwd used by the wrapper. |
| `entrypoint` | CONTRACT | Wrapper entrypoint path relative to the skillbox repo. |
| `next_actions` | info | Common first follow-up commands. |
| `ok` | CONTRACT | True when the wrapper emitted a complete capabilities payload. |
| `safety` | CONTRACT | Dry-run and confirmation guidance for mutating commands. |
| `skill_verbs` | CONTRACT | Machine-readable skill verb decision map; every dispatched skill subcommand has an entry. |
| `stdout_stderr_contract` | CONTRACT | Where JSON and diagnostics are emitted. |
| `tool` | CONTRACT | Tool identity, e.g. skillbox-sbp. |

#### `skill_verbs.<verb>` (one skill verb row)

| Field | Stability | Meaning |
|-------|-----------|---------|
| `do_NOT` | info | Important anti-pattern for this verb. |
| `links_disk` | CONTRACT | True when the verb may create/remove skill links on disk. |
| `mutates` | CONTRACT | Stable mutation class: none, cwd-ephemeral, disk-links, or repo-state+disk-links. |
| `purpose` | CONTRACT | One-line meaning of the verb. |
| `returns_packet` | CONTRACT | True when success includes an activation_packet for immediate session use. |
| `scope` | CONTRACT | Scope the verb operates on. |
| `survives_recalibrate` | CONTRACT | True when the verb writes durable repo state that recalibrate/prune should preserve. |
| `when_to_use` | info | Human/agent guidance for choosing this verb. |

### Example payload

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>` / `<BR_BIN>` / `<REMOTE_ROOT>`.</sub>

```json
{
  "agent_surfaces": {
    "capabilities": "sbp capabilities --json",
    "json_aliases": [
      "--jason",
      "--json",
      "--jsno",
      "--jsson"
    ],
    "robot_docs": "sbp robot-docs guide",
    "robot_triage": "sbp --robot-triage"
  },
  "aliases": [
    "sbo"
  ],
  "commands": [
    {
      "json": true,
      "name": "capabilities",
      "safe_first_try": "sbp capabilities --json"
    },
    {
      "json": true,
      "name": "robot-docs",
      "safe_first_try": "sbp robot-docs guide --json"
    },
    {
      "json": true,
      "name": "robot-triage",
      "safe_first_try": "sbp --robot-triage"
    },
    {
      "json": true,
      "name": "status",
      "safe_first_try": "sbp status --json"
    },
    {
      "json": true,
      "name": "logs",
      "safe_first_try": "sbp logs <profile> <service> --json"
    },
    {
      "json": true,
      "name": "up",
      "safe_first_try": "sbp up <profile> <service> --dry-run --json"
    },
    {
      "json": true,
      "name": "down",
      "safe_first_try": "sbp down <profile> <service> --dry-run --json"
    },
    {
      "json": true,
      "name": "restart",
      "safe_first_try": "sbp restart <profile> <service> --dry-run --json"
    },
    {
      "aliases": [
        "bulk"
      ],
      "json": true,
      "name": "launch",
      "safe_first_try": "sbp launch <dir> <dir> --request '<prompt>' --dry-run --json"
    },
    {
      "alias_for": "launch",
      "json": true,
      "name": "bulk",
      "safe_first_try": "sbp bulk <dir> <dir> --request '<prompt>' --dry-run --json"
    },
    {
      "json": true,
      "name": "skills",
      "safe_first_try": "sbp skills --issues-only --json"
    },
    {
      "json": true,
      "name": "skill-why",
      "safe_first_try": "sbp skill why <skill> --json"
    },
    {
      "json": true,
      "name": "skill-togglable",
      "safe_first_try": "sbp skill togglable --json"
    },
    {
      "json": true,
      "name": "skill-what-if",
      "safe_first_try": "sbp skill what-if --repo <repo-id-or-path> --overlay <overlay> --json"
    },
    {
      "json": true,
      "name": "skill-heal",
      "safe_first_try": "sbp skill heal <skill> --dry-run --format json"
    },
    {
      "json": true,
      "name": "candidates",
      "safe_first_try": "sbp candidates --json"
    },
    {
      "json": true,
      "name": "mcp",
      "safe_first_try": "sbp mcp --json"
    },
    {
      "json": true,
      "name": "registry",
      "safe_first_try": "sbp registry doctor --json"
    },
    {
      "fallback": "If status is error/degraded/stale during active work, report degraded_cass evidence mode and use the local transcript scanner; do not rebuild Cass mid-task.",
      "json": true,
      "name": "cass",
      "safe_first_try": "sbp cass status --json"
    },
    {
      "json": true,
      "name": "evidence",
      "safe_first_try": "sbp evidence --repo <path> --format json"
    },
    {
      "json": true,
      "name": "cron",
      "safe_first_try": "sbp cron status --json"
    },
    {
      "json": true,
      "name": "send-later",
      "safe_first_try": "sbp send-later list --json"
    },
    {
      "json": true,
      "name": "recalibrate",
      "safe_first_try": "sbp recalibrate --json"
    }
  ],
  "contract_version": "2026-05-11",
  "cwd": "<RUNTIME_ROOT>",
  "entrypoint": "scripts/sbp",
  "next_actions": [
    "sbp status --json",
    "sbp skills --issues-only --json",
    "sbp mcp --json",
    "sbp launch <dir> <dir> --request '<prompt>' --dry-run --json",
    "sbp up <profile> <service> --dry-run --json"
  ],
  "ok": true,
  "safety": {
    "confirm_with_user_before": [
      "sbp down <profile> <service>"
    ],
    "dry_run_first": [
      "sbp up <profile> <service> --dry-run --json",
      "sbp down <profile> <service> --dry-run --json",
      "sbp restart <profile> <service> --dry-run --json",
      "sbp launch <dir> <dir> --request '<prompt>' --dry-run --json",
      "sbp bulk <dir> <dir> --request '<prompt>' --dry-run --json",
      "sbp skill prune --dry-run",
      "sbp skill default on <skill> --repo --dry-run --format json"
    ]
  },
  "skill_verbs": {
    "activate": {
      "do_NOT": "Do not treat activate as durable repo state; use on or heal for that.",
      "links_disk": true,
      "mutates": "cwd-ephemeral",
      "purpose": "Install/link a skill and print an activation packet for this session.",
      "returns_packet": true,
      "scope": "current cwd by default; global/category if explicitly requested",
      "survives_recalibrate": false,
      "when_to_use": "Use for an immediate one-session handoff when durability is not required."
    },
    "add": {
      "do_NOT": "Do not use add when the repo needs a durable policy override; use on or heal.",
      "links_disk": true,
      "mutates": "disk-links",
      "purpose": "Install/link a skill into a selected scope.",
      "returns_packet": false,
      "scope": "global, project, or category",
      "survives_recalibrate": false,
      "when_to_use": "Use for deliberate non-override link management."
    },
    "default": {
      "do_NOT": "Do not apply --global without --dry-run review and --yes; it writes operator skill-scope policy.",
      "links_disk": false,
      "mutates": "repo_or_operator_policy",
      "purpose": "Set repo or operator-global skill defaults with a reviewable unified diff.",
      "returns_packet": false,
      "scope": "repo or global",
      "survives_recalibrate": true,
      "when_to_use": "Use --repo for a committed repo default; use --global only after dry-run review."
    },
    "heal": {
      "do_NOT": "Do not use heal when no real source exists; unknown sources are refused.",
      "links_disk": true,
      "mutates": "repo-state+disk-links",
      "purpose": "Resolve a real skill source, durably pin it on, link it, and return an activation packet.",
      "returns_packet": true,
      "scope": "repo-local project scope",
      "survives_recalibrate": true,
      "when_to_use": "Use when a source-backed skill is missing and should become visible now and later."
    },
    "lint": {
      "do_NOT": "Do not expect lint to repair or rewrite the file.",
      "links_disk": false,
      "mutates": "none",
      "purpose": "Validate the repo-local .skillbox/skill-overrides.yaml file.",
      "returns_packet": false,
      "scope": "current repo override file",
      "survives_recalibrate": false,
      "when_to_use": "Use after editing override state or when a pin behaves unexpectedly."
    },
    "move": {
      "do_NOT": "Do not use move as a policy override; it only manages links.",
      "links_disk": true,
      "mutates": "disk-links",
      "purpose": "Install/link a skill into a new scope and remove old installs for that skill.",
      "returns_packet": false,
      "scope": "global, project, or category",
      "survives_recalibrate": false,
      "when_to_use": "Use when intentionally relocating an existing install."
    },
    "off": {
      "do_NOT": "Do not use off to disable dispatcher floor skills such as smart or sbp.",
      "links_disk": true,
      "mutates": "repo-state+disk-links",
      "purpose": "Durably pin a skill off for this repo and unlink project installs.",
      "returns_packet": false,
      "scope": "repo-local project scope",
      "survives_recalibrate": true,
      "when_to_use": "Use when a repo should keep a skill disabled."
    },
    "on": {
      "do_NOT": "Do not use on for global escalation; disallowed globals are refused.",
      "links_disk": true,
      "mutates": "repo-state+disk-links",
      "purpose": "Durably pin a skill on for this repo, link it, and return an activation packet.",
      "returns_packet": true,
      "scope": "repo-local project scope",
      "survives_recalibrate": true,
      "when_to_use": "Use when the repo should keep seeing a known source-backed skill."
    },
    "plan": {
      "do_NOT": "Do not expect plan to make a skill visible.",
      "links_disk": false,
      "mutates": "none",
      "purpose": "Preview where a skill lifecycle operation would install or remove links.",
      "returns_packet": false,
      "scope": "global, project, or category preview",
      "survives_recalibrate": false,
      "when_to_use": "Use before a risky link or move when you only need the plan."
    },
    "prune": {
      "do_NOT": "Do not skip dry-run; pinned overrides are protected by the prune firewall.",
      "links_disk": true,
      "mutates": "disk-links",
      "purpose": "Remove installed skills that violate current skill-scope policy.",
      "returns_packet": false,
      "scope": "global, project, or all selected installs",
      "survives_recalibrate": false,
      "when_to_use": "Use after dry-run to remove drift that policy does not allow."
    },
    "recalibrate": {
      "do_NOT": "Do not treat bare recalibrate as a mutator; --auto-fix previews and --auto-fix --yes applies heal.",
      "links_disk": false,
      "mutates": "none",
      "purpose": "Read-only cwd/fleet skill visibility audit with exact next commands; --auto-fix previews heal, --yes applies.",
      "returns_packet": false,
      "scope": "cwd or fleet",
      "survives_recalibrate": false,
      "when_to_use": "Use first when unsure what the repo or fleet needs."
    },
    "remove": {
      "do_NOT": "Do not use remove when policy should keep the skill absent; use off.",
      "links_disk": true,
      "mutates": "disk-links",
      "purpose": "Remove installed links/files for a skill.",
      "returns_packet": false,
      "scope": "global, project, or all selected installs",
      "survives_recalibrate": false,
      "when_to_use": "Use for direct cleanup of installed links."
    },
    "sync": {
      "do_NOT": "Do not use sync to create a durable exception; use on or heal.",
      "links_disk": true,
      "mutates": "disk-links",
      "purpose": "Install/link a named skill or all literal skills missing for the current cwd policy.",
      "returns_packet": false,
      "scope": "current cwd policy, project by default",
      "survives_recalibrate": false,
      "when_to_use": "Use when policy already requires a skill and only links are missing."
    },
    "togglable": {
      "do_NOT": "Do not use togglable as proof a flip already happened; run the returned command to mutate state.",
      "links_disk": false,
      "mutates": "none",
      "purpose": "List every skill flippable at this cwd and the exact command to flip it.",
      "returns_packet": false,
      "scope": "current cwd",
      "survives_recalibrate": false,
      "when_to_use": "Use when you need a one-read switchboard of repo skill on/off affordances."
    },
    "toggleable": {
      "do_NOT": "Do not use toggleable as proof a flip already happened; run the returned command to mutate state.",
      "links_disk": false,
      "mutates": "none",
      "purpose": "Alias for togglable; lists every skill flippable at this cwd and the exact command to flip it.",
      "returns_packet": false,
      "scope": "current cwd",
      "survives_recalibrate": false,
      "when_to_use": "Use when you need a one-read switchboard of repo skill on/off affordances."
    },
    "what-if": {
      "do_NOT": "Do not treat what-if as applying anything; it writes zero files.",
      "links_disk": false,
      "mutates": "none",
      "purpose": "Purely simulate effective skill visibility for a repo, overlay, pin, opt-out, and machine.",
      "returns_packet": false,
      "scope": "target repo from --repo",
      "survives_recalibrate": false,
      "when_to_use": "Use before overlay or pin changes to see added, removed, shadowed, and conflict results."
    },
    "why": {
      "do_NOT": "Do not infer policy from memory when why can return the live layers.",
      "links_disk": false,
      "mutates": "none",
      "purpose": "Explain one skill's visibility provenance, absence, and exact fixes.",
      "returns_packet": false,
      "scope": "current cwd",
      "survives_recalibrate": false,
      "when_to_use": "Use when choosing the narrowest correct fix for one skill."
    }
  },
  "stdout_stderr_contract": {
    "diagnostics_stderr": "JSON typo alias notices and parser errors go to stderr.",
    "json_stdout": "When JSON is requested, stdout is parseable JSON from manage.py or this wrapper."
  },
  "tool": "skillbox-sbp"
}
```

---

## `sbp skills`

**Invocation:** `sbp skills [--full] [--no-global] [--show-sources] --format json`
**Produced by:** `collect_skill_visibility (compact via compact_skill_visibility_payload)`

The conflict-aware skill availability view for the current cwd. `sbp skills` emits the COMPACT payload (below); `sbp skills --full` adds `global_surfaces`, `layers`, and `occurrences`. Branch on `summary` counters and `issues` groups; `effective` is the authoritative list of what is visible here.

### Fields

| Field | Stability | Meaning |
|-------|-----------|---------|
| `cwd` | CONTRACT | Absolute resolved cwd the visibility view was computed for. |
| `active_clients` | CONTRACT | Client overlays active for this resolution. |
| `active_profiles` | CONTRACT | Runtime profiles active for this resolution. |
| `matched_clients` | CONTRACT | Client overlays whose cwd_match matched this cwd (id + match). |
| `matched_project_categories` | CONTRACT | Policy project categories this cwd falls under (id + path). |
| `matched_scope_rules` | CONTRACT | skill-scope.yaml rules in force for this cwd (id + provenance). |
| `summary` | CONTRACT | Roll-up counters; keys are stable, add-only. Branch on these first. |
| `parity` | CONTRACT | Claude<->Codex GLOBAL skill-surface parity (empty when --no-global). |
| `visibility_decisions` | CONTRACT | One winning resolution row per skill name, including disabled/broken winners; use effective for visible skills. |
| `effective` | CONTRACT | Visible skills at this cwd after layer resolution; excludes disabled/broken winners. |
| `issues` | CONTRACT | Policy problems grouped by kind (broken_project, missing_for_cwd, scope_violations, ...). |
| `beads` | CONTRACT | Beads requirement/readiness derived from effective skills' frontmatter. |
| `recommendations` | info | Ranked human-facing remediation suggestions. |
| `policy` | CONTRACT | Which policy files + project categories drove this view. |
| `source_roots` | info | Discovered skill source roots (only when --show-sources). |
| `undefined_sources` | info | Linkable sources with no policy occurrence (only when --show-sources). |
| `next_actions` | info | Ordered, copy-pasteable next commands for a human/agent. |

### Example payload

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>` / `<BR_BIN>` / `<REMOTE_ROOT>`.</sub>

```json
{
  "active_clients": [],
  "active_profiles": [
    "core"
  ],
  "beads": {
    "beads_dir": "<FLEET>/repos_real/overlay-repo/.beads",
    "br": "<BR_BIN>",
    "initialized": false,
    "issues": [],
    "next_actions": [],
    "ok": true,
    "repo_root": "<FLEET>/repos_real/overlay-repo",
    "required": false,
    "required_skills": []
  },
  "cwd": "<FLEET>/repos_real/overlay-repo",
  "effective": [
    {
      "availability": "installed",
      "layer": "project:claude:<FLEET>/repos_real/overlay-repo",
      "name": "tiny-marketing",
      "path": "<FLEET>/repos_real/overlay-repo/.claude/skills/tiny-marketing",
      "shadowed_count": 0,
      "source": "<FLEET>/private-skills/tiny-marketing",
      "source_bucket": "external",
      "state": "ok",
      "winning_layer": "project:claude:<FLEET>/repos_real/overlay-repo"
    }
  ],
  "issues": {
    "archive_sources": [],
    "broken_global": [],
    "broken_project": [],
    "extra_global": [],
    "global_not_allowed": [],
    "missing_for_cwd": [
      {
        "allowed_paths": [
          "<FLEET>/repos_real/overlay-repo"
        ],
        "categories": [
          "frontend"
        ],
        "fix_command": "sbp skill on tiny-ui --cwd $PWD",
        "name": "tiny-ui",
        "origin": null,
        "policy_path": "<FLEET>/skillbox-config/skill-scope.yaml",
        "reason": "skill is expected for this cwd but is not currently effective",
        "rule_id": "frontend-local",
        "scope_policy_path": "<FLEET>/skillbox-config/skill-scope.yaml",
        "scope_rule": "frontend-local",
        "type": "missing_for_cwd"
      }
    ],
    "scope_violations": [],
    "shadowed": []
  },
  "matched_clients": [],
  "matched_project_categories": [
    {
      "id": "frontend",
      "match": "<FLEET>/repos_real/overlay-repo",
      "notes": "",
      "paths": [
        "<FLEET>/repos_real/overlay-repo"
      ],
      "policy_path": "<FLEET>/skillbox-config/skill-scope.yaml"
    }
  ],
  "matched_scope_rules": [
    {
      "activation": "",
      "allow_global": false,
      "categories": [
        "frontend"
      ],
      "default": "on",
      "id": "frontend-local",
      "match": "<FLEET>/repos_real/overlay-repo",
      "notes": "",
      "overlay": "",
      "path_match": "prefix",
      "paths": [
        "<FLEET>/repos_real/overlay-repo"
      ],
      "patterns": [
        "tiny-ui"
      ],
      "policy_path": "<FLEET>/skillbox-config/skill-scope.yaml",
      "repos": [],
      "unknown_categories": []
    }
  ],
  "next_actions": [
    "add missing cwd-scoped skills to the active client or project skill-repos.yaml"
  ],
  "parity": {},
  "policy": {
    "files": [
      "<FLEET>/skillbox-config/skill-scope.yaml"
    ],
    "project_categories": [
      {
        "id": "cli",
        "notes": "",
        "paths": [
          "<FLEET>/repos_real/healthy"
        ],
        "policy_path": "<FLEET>/skillbox-config/skill-scope.yaml"
      },
      {
        "id": "frontend",
        "notes": "",
        "paths": [
          "<FLEET>/repos_real/overlay-repo"
        ],
        "policy_path": "<FLEET>/skillbox-config/skill-scope.yaml"
      }
    ]
  },
  "recommendations": [
    {
      "action": "add_project_skill",
      "allowed_paths": [
        "<FLEET>/repos_real/overlay-repo"
      ],
      "fix_command": "sbp skill on tiny-ui --cwd $PWD",
      "hint": "Add this skill to the active client's skill-repos.yaml, or durably pin it for this repo with `sbp skill on <skill> --cwd $PWD`. Use `sbp overlay activate <name> --cwd <repo>` for a one-session/cwd policy-evaluated flip, or `sbp overlay on <name>` to PERSIST the overlay across sessions until `overlay off`.",
      "issue_type": "missing_for_cwd",
      "origin": null,
      "policy_path": "<FLEET>/skillbox-config/skill-scope.yaml",
      "rule_id": "frontend-local",
      "scope_rule": "frontend-local",
      "skill": "tiny-ui",
      "target": "project_or_client_skill_repos"
    }
  ],
  "source_roots": [],
  "summary": {
    "archive_source_skills": 0,
    "archive_sources": 0,
    "beads_issues": 0,
    "beads_required_skills": 0,
    "broken_by_class": {
      "dangling": 0,
      "moved": 0,
      "other-machine": 0,
      "unreadable": 0
    },
    "broken_global": 0,
    "broken_global_skills": 0,
    "broken_project": 0,
    "broken_project_skills": 0,
    "effective": 1,
    "extra_global": 0,
    "extra_global_skills": 0,
    "global_not_allowed": 0,
    "global_not_allowed_skills": 0,
    "layers": 3,
    "missing_for_cwd": 1,
    "missing_for_cwd_skills": 1,
    "occurrences": 2,
    "parity_divergent": 0,
    "recommendations": 1,
    "scope_violation_skills": 0,
    "scope_violations": 0,
    "shadowed": 0,
    "undeclared_active_overlays": 0,
    "undefined_source_skills": 0,
    "undefined_sources": 0
  },
  "undefined_sources": [],
  "visibility_decisions": [
    {
      "availability": "override",
      "layer": "repo-override-file",
      "name": "tiny-cli",
      "path": "<FLEET>/repos_real/overlay-repo",
      "shadowed_count": 0,
      "source": "<FLEET>/skills/tiny-cli",
      "source_bucket": "external",
      "state": "disabled",
      "winning_layer": "repo-override-file"
    },
    {
      "availability": "installed",
      "layer": "project:claude:<FLEET>/repos_real/overlay-repo",
      "name": "tiny-marketing",
      "path": "<FLEET>/repos_real/overlay-repo/.claude/skills/tiny-marketing",
      "shadowed_count": 0,
      "source": "<FLEET>/private-skills/tiny-marketing",
      "source_bucket": "external",
      "state": "ok",
      "winning_layer": "project:claude:<FLEET>/repos_real/overlay-repo"
    }
  ]
}
```

---

## `sbp candidates`

**Invocation:** `sbp candidates --json` (== `sbp skills --show-sources --full --no-global --format json`)
**Produced by:** `collect_skill_visibility (full, include_sources=True)`

The exploratory source-inventory surface. Same payload as `sbp skills --full` with sources enabled; the load-bearing fields for bucketing candidates are `undefined_sources` + `source_roots` (the linkable universe) against `effective` (already present), `issues.missing_for_cwd` (definitely), and the matched policy.

### Fields

| Field | Stability | Meaning |
|-------|-----------|---------|
| `cwd` | CONTRACT | Absolute resolved cwd the visibility view was computed for. |
| `matched_clients` | CONTRACT | Client overlays whose cwd_match matched this cwd (id + match). |
| `matched_project_categories` | CONTRACT | Policy project categories this cwd falls under (id + path). |
| `matched_scope_rules` | CONTRACT | skill-scope.yaml rules in force for this cwd (id + provenance). |
| `active_clients` | CONTRACT | Client overlays active for this resolution. |
| `active_profiles` | CONTRACT | Runtime profiles active for this resolution. |
| `global_surfaces` | info | Per-surface GLOBAL home skill report (only with global scope). |
| `parity` | CONTRACT | Claude<->Codex GLOBAL skill-surface parity (empty when --no-global). |
| `layers` | info | Every resolution layer considered, ranked (full payload only). |
| `source_roots` | CONTRACT | Every skill source root discovered under the configured roots — the linkable universe. |
| `visibility_decisions` | CONTRACT | One winning resolution row per skill name, including disabled/broken winners; use effective for visible skills. |
| `effective` | CONTRACT | Visible skills at this cwd after layer resolution; excludes disabled/broken winners. |
| `occurrences` | CONTRACT | Every raw skill occurrence across all layers (full payload only). |
| `undefined_sources` | CONTRACT | Linkable source skills with no policy occurrence — the candidate pool. |
| `beads` | CONTRACT | Beads requirement/readiness derived from effective skills' frontmatter. |
| `issues` | CONTRACT | Policy problems grouped by kind (broken_project, missing_for_cwd, scope_violations, ...). |
| `policy` | CONTRACT | Which policy files + project categories drove this view. |
| `overlay_audit` | info | Declared-overlay registry audit: declared + active overlays and warnings for active overlays not in the registry (advisory, never a hard fail; only when an overlays: block is declared). |
| `recommendations` | info | Ranked human-facing remediation suggestions. |
| `summary` | CONTRACT | Roll-up counters; keys are stable, add-only. Branch on these first. |
| `next_actions` | info | Ordered, copy-pasteable next commands for a human/agent. |

### Example payload

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>` / `<BR_BIN>` / `<REMOTE_ROOT>`.</sub>

```json
{
  "active_clients": [],
  "active_profiles": [
    "core"
  ],
  "beads": {
    "beads_dir": "<FLEET>/repos_real/healthy/.beads",
    "br": "<BR_BIN>",
    "initialized": false,
    "issues": [
      {
        "code": "no_beads_dir",
        "hint": "sbp beads init --cwd <FLEET>/repos_real/healthy",
        "message": "BEADS DRIFT: 1 active skill(s) require .beads/ in this repo"
      }
    ],
    "next_actions": [
      "sbp beads init --cwd <FLEET>/repos_real/healthy"
    ],
    "ok": false,
    "repo_root": "<FLEET>/repos_real/healthy",
    "required": true,
    "required_skills": [
      {
        "layer": "repo-override-file",
        "name": "needs-beads",
        "source": "<FLEET>/private-skills/needs-beads"
      }
    ]
  },
  "cwd": "<FLEET>/repos_real/healthy",
  "effective": [
    {
      "availability": "override",
      "layer": "repo-override-file",
      "layer_label": "repo override file",
      "layer_rank": 60,
      "name": "needs-beads",
      "override_action": "pin_on",
      "path": "<FLEET>/repos_real/healthy",
      "policy_path": "<FLEET>/repos_real/healthy/.skillbox/skill-overrides.yaml",
      "scope": "repo",
      "shadowed_count": 0,
      "source": "<FLEET>/private-skills/needs-beads",
      "source_bucket": "external",
      "state": "pinned",
      "winning_layer": "repo-override-file"
    },
    {
      "availability": "installed",
      "has_skill_md": true,
      "layer": "project:claude:<FLEET>/repos_real/healthy",
      "layer_label": "project claude",
      "layer_rank": 40,
      "link_target": "<FLEET>/skills/tiny-cli",
      "name": "tiny-cli",
      "path": "<FLEET>/repos_real/healthy/.claude/skills/tiny-cli",
      "scope": "installed",
      "shadowed_count": 0,
      "source": "<FLEET>/skills/tiny-cli",
      "source_bucket": "external",
      "source_kind": "directory",
      "state": "ok",
      "winning_layer": "project:claude:<FLEET>/repos_real/healthy"
    }
  ],
  "global_surfaces": [],
  "issues": {
    "archive_sources": [],
    "broken_global": [],
    "broken_project": [],
    "extra_global": [],
    "global_not_allowed": [],
    "missing_for_cwd": [],
    "scope_violations": [],
    "shadowed": []
  },
  "layers": [
    {
      "broken_count": 0,
      "id": "project:claude:<FLEET>/repos_real/healthy",
      "kind": "installed",
      "label": "project claude",
      "non_skill_count": 0,
      "path": "<FLEET>/repos_real/healthy/.claude/skills",
      "present": true,
      "rank": 40,
      "skill_count": 1
    },
    {
      "broken_count": 0,
      "id": "project:codex:<FLEET>/repos_real/healthy",
      "kind": "installed",
      "label": "project codex",
      "non_skill_count": 0,
      "path": "<FLEET>/repos_real/healthy/.codex/skills",
      "present": true,
      "rank": 40,
      "skill_count": 0
    },
    {
      "id": "repo-override-file",
      "kind": "override",
      "label": "repo override file",
      "path": "<FLEET>/repos_real/healthy/.skillbox/skill-overrides.yaml",
      "present": true,
      "rank": 60,
      "scope": "repo",
      "skill_count": 3,
      "vetoed_floor": []
    }
  ],
  "matched_clients": [],
  "matched_project_categories": [
    {
      "id": "cli",
      "match": "<FLEET>/repos_real/healthy",
      "notes": "",
      "paths": [
        "<FLEET>/repos_real/healthy"
      ],
      "policy_path": "<FLEET>/skillbox-config/skill-scope.yaml"
    }
  ],
  "matched_scope_rules": [
    {
      "activation": "",
      "allow_global": false,
      "categories": [
        "cli"
      ],
      "default": "on",
      "id": "cli-local",
      "match": "<FLEET>/repos_real/healthy",
      "notes": "",
      "overlay": "",
      "path_match": "prefix",
      "paths": [
        "<FLEET>/repos_real/healthy"
      ],
      "patterns": [
        "tiny-cli"
      ],
      "policy_path": "<FLEET>/skillbox-config/skill-scope.yaml",
      "repos": [],
      "unknown_categories": []
    }
  ],
  "next_actions": [
    "doctor --format json",
    "sbp beads init --cwd <FLEET>/repos_real/healthy"
  ],
  "occurrences": [
    {
      "availability": "installed",
      "has_skill_md": true,
      "layer": "project:claude:<FLEET>/repos_real/healthy",
      "layer_label": "project claude",
      "layer_rank": 40,
      "link_target": "<FLEET>/skills/tiny-cli",
      "name": "tiny-cli",
      "path": "<FLEET>/repos_real/healthy/.claude/skills/tiny-cli",
      "scope": "installed",
      "source": "<FLEET>/skills/tiny-cli",
      "source_bucket": "external",
      "source_kind": "directory",
      "state": "ok"
    },
    {
      "availability": "override",
      "layer": "repo-override-file",
      "layer_label": "repo override file",
      "layer_rank": 60,
      "name": "needs-beads",
      "override_action": "pin_on",
      "path": "<FLEET>/repos_real/healthy",
      "policy_path": "<FLEET>/repos_real/healthy/.skillbox/skill-overrides.yaml",
      "scope": "repo",
      "source": "<FLEET>/private-skills/needs-beads",
      "source_bucket": "external",
      "state": "pinned"
    },
    {
      "availability": "override",
      "layer": "repo-override-file",
      "layer_label": "repo override file",
      "layer_rank": 60,
      "name": "tiny-marketing",
      "override_action": "pin_off",
      "path": "<FLEET>/repos_real/healthy",
      "policy_path": "<FLEET>/repos_real/healthy/.skillbox/skill-overrides.yaml",
      "scope": "repo",
      "source": "<FLEET>/private-skills/tiny-marketing",
      "source_bucket": "external",
      "state": "disabled"
    },
    {
      "availability": "override",
      "layer": "repo-override-file:global-opt-out",
      "layer_label": "repo override global opt-out",
      "layer_rank": 35,
      "name": "fixture-global-optout",
      "override_action": "opt_out_global",
      "path": "<FLEET>/repos_real/healthy",
      "policy_path": "<FLEET>/repos_real/healthy/.skillbox/skill-overrides.yaml",
      "scope": "repo",
      "source": null,
      "source_bucket": null,
      "state": "disabled"
    }
  ],
  "overlay_audit": {
    "active": [
      "marketing"
    ],
    "active_layers": [
      {
        "enabled": true,
        "layer": "repo-override-file",
        "layer_label": "repo override file",
        "layer_rank": 60,
        "name": "marketing",
        "source": "<FLEET>/repos_real/healthy/.skillbox/skill-overrides.yaml",
        "why": "repo-file"
      },
      {
        "enabled": false,
        "layer": "repo-override-file",
        "layer_label": "repo override file",
        "layer_rank": 60,
        "name": "swarm",
        "source": "<FLEET>/repos_real/healthy/.skillbox/skill-overrides.yaml",
        "why": "repo-file"
      }
    ],
    "declared": [],
    "undeclared_active": [],
    "warnings": []
  },
  "parity": {},
  "policy": {
    "files": [
      "<FLEET>/skillbox-config/skill-scope.yaml"
    ],
    "project_categories": [
      {
        "id": "cli",
        "notes": "",
        "paths": [
          "<FLEET>/repos_real/healthy"
        ],
        "policy_path": "<FLEET>/skillbox-config/skill-scope.yaml"
      },
      {
        "id": "frontend",
        "notes": "",
        "paths": [
          "<FLEET>/repos_real/overlay-repo"
        ],
        "policy_path": "<FLEET>/skillbox-config/skill-scope.yaml"
      }
    ]
  },
  "recommendations": [],
  "source_roots": [
    {
      "id": "source:<FLEET>/private-skills",
      "kind": "source",
      "label": "<FLEET>/private-skills",
      "path": "<FLEET>/private-skills",
      "present": true,
      "rank": 0,
      "skill_count": 2,
      "undefined_count": 0
    },
    {
      "id": "source:<FLEET>/skills",
      "kind": "source",
      "label": "<FLEET>/skills",
      "path": "<FLEET>/skills",
      "present": true,
      "rank": 0,
      "skill_count": 2,
      "undefined_count": 0
    }
  ],
  "summary": {
    "archive_source_skills": 0,
    "archive_sources": 0,
    "beads_issues": 1,
    "beads_required_skills": 1,
    "broken_by_class": {
      "dangling": 0,
      "moved": 0,
      "other-machine": 0,
      "unreadable": 0
    },
    "broken_global": 0,
    "broken_global_skills": 0,
    "broken_project": 0,
    "broken_project_skills": 0,
    "effective": 2,
    "extra_global": 0,
    "extra_global_skills": 0,
    "global_not_allowed": 0,
    "global_not_allowed_skills": 0,
    "layers": 3,
    "missing_for_cwd": 0,
    "missing_for_cwd_skills": 0,
    "occurrences": 4,
    "parity_divergent": 0,
    "recommendations": 0,
    "scope_violation_skills": 0,
    "scope_violations": 0,
    "shadowed": 0,
    "undeclared_active_overlays": 0,
    "undefined_source_skills": 0,
    "undefined_sources": 0
  },
  "undefined_sources": [],
  "visibility_decisions": [
    {
      "availability": "override",
      "layer": "repo-override-file:global-opt-out",
      "layer_label": "repo override global opt-out",
      "layer_rank": 35,
      "name": "fixture-global-optout",
      "override_action": "opt_out_global",
      "path": "<FLEET>/repos_real/healthy",
      "policy_path": "<FLEET>/repos_real/healthy/.skillbox/skill-overrides.yaml",
      "scope": "repo",
      "shadowed_count": 0,
      "source": null,
      "source_bucket": null,
      "state": "disabled",
      "winning_layer": "repo-override-file:global-opt-out"
    },
    {
      "availability": "override",
      "layer": "repo-override-file",
      "layer_label": "repo override file",
      "layer_rank": 60,
      "name": "needs-beads",
      "override_action": "pin_on",
      "path": "<FLEET>/repos_real/healthy",
      "policy_path": "<FLEET>/repos_real/healthy/.skillbox/skill-overrides.yaml",
      "scope": "repo",
      "shadowed_count": 0,
      "source": "<FLEET>/private-skills/needs-beads",
      "source_bucket": "external",
      "state": "pinned",
      "winning_layer": "repo-override-file"
    },
    {
      "availability": "installed",
      "has_skill_md": true,
      "layer": "project:claude:<FLEET>/repos_real/healthy",
      "layer_label": "project claude",
      "layer_rank": 40,
      "link_target": "<FLEET>/skills/tiny-cli",
      "name": "tiny-cli",
      "path": "<FLEET>/repos_real/healthy/.claude/skills/tiny-cli",
      "scope": "installed",
      "shadowed_count": 0,
      "source": "<FLEET>/skills/tiny-cli",
      "source_bucket": "external",
      "source_kind": "directory",
      "state": "ok",
      "winning_layer": "project:claude:<FLEET>/repos_real/healthy"
    },
    {
      "availability": "override",
      "layer": "repo-override-file",
      "layer_label": "repo override file",
      "layer_rank": 60,
      "name": "tiny-marketing",
      "override_action": "pin_off",
      "path": "<FLEET>/repos_real/healthy",
      "policy_path": "<FLEET>/repos_real/healthy/.skillbox/skill-overrides.yaml",
      "scope": "repo",
      "shadowed_count": 0,
      "source": "<FLEET>/private-skills/tiny-marketing",
      "source_bucket": "external",
      "state": "disabled",
      "winning_layer": "repo-override-file"
    }
  ]
}
```

---

## `sbp mcp`

**Invocation:** `sbp mcp [--cwd <repo>] --format json` (bare `sbp mcp` runs the read-only audit)
**Produced by:** `collect_mcp_audit`

Claude (`.mcp.json`) vs Codex (`.codex/config.toml`) MCP-server reconciliation. `expected_servers` is the per-scope baseline, `declared_servers` adds model-declared servers (any profile). Gate on `summary.unexplained_drift` and `summary.invalid_configs`; per-surface detail is in `surfaces.claude` / `surfaces.codex`.

### Fields

| Field | Stability | Meaning |
|-------|-----------|---------|
| `cwd` | CONTRACT | Absolute resolved cwd the audit ran against. |
| `config_root` | CONTRACT | Repo whose Claude/Codex MCP config is audited. |
| `expected_servers` | CONTRACT | Servers expected for this profile/scope (sorted). |
| `declared_servers` | CONTRACT | Servers explained by the runtime model (any profile) ∪ expected. |
| `surfaces` | CONTRACT | Per-surface (claude/codex) config read; see the surface field table. |
| `parity` | CONTRACT | Claude-vs-Codex set difference, split declared (intentional) vs unexpected (drift). |
| `summary` | CONTRACT | Counters; unexplained_drift>0 and invalid_configs>0 are the gate signals. |
| `next_actions` | info | Ordered repair commands for missing/unexpected/parity drift. |

#### `surfaces.claude` / `surfaces.codex` (one MCP surface)

| Field | Stability | Meaning |
|-------|-----------|---------|
| `name` | CONTRACT | Surface id: 'claude' or 'codex'. |
| `format` | CONTRACT | Config format: 'json' (Claude) or 'toml' (Codex). |
| `path` | CONTRACT | Absolute path of the surface config file. |
| `present` | CONTRACT | Whether the config file exists as a readable file. |
| `broken_symlink` | CONTRACT | True when path is a symlink whose target is absent. |
| `symlink_target` | info | Raw readlink target when path is a symlink, else null. |
| `valid` | CONTRACT | False when the config could not be parsed (see error). |
| `servers` | CONTRACT | All declared server names (incl. disabled), sorted. |
| `effective_servers` | CONTRACT | Enabled server names (servers minus disabled), sorted. |
| `disabled_servers` | CONTRACT | Server names present but disabled. |
| `missing` | CONTRACT | expected_servers absent from this surface — the add list. |
| `extra` | info | effective_servers not in expected (declared + unexplained combined). |
| `extra_intentional` | CONTRACT | Extra servers that ARE declared (profile-gated; not drift). |
| `unexpected` | CONTRACT | Servers present but neither expected nor declared — real drift. |
| `error` | CONTRACT | Parse error string, or null when valid. |

### Example payload

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>` / `<BR_BIN>` / `<REMOTE_ROOT>`.</sub>

```json
{
  "config_root": "<FLEET>/repos_real/healthy",
  "cwd": "<FLEET>/repos_real/healthy",
  "declared_servers": [
    "skillbox"
  ],
  "expected_servers": [
    "skillbox"
  ],
  "next_actions": [
    "add skillbox to <FLEET>/repos_real/healthy/.mcp.json",
    "add skillbox to <FLEET>/repos_real/healthy/.codex/config.toml"
  ],
  "parity": {
    "claude_only": [],
    "claude_only_declared": [],
    "claude_only_unexpected": [],
    "codex_only": [],
    "codex_only_declared": [],
    "codex_only_unexpected": [],
    "shared": []
  },
  "summary": {
    "claude_extra": 0,
    "claude_missing": 1,
    "claude_only": 0,
    "claude_unexpected": 0,
    "codex_extra": 0,
    "codex_missing": 1,
    "codex_only": 0,
    "codex_unexpected": 0,
    "declared": 1,
    "expected": 1,
    "invalid_configs": 0,
    "unexplained_drift": 0
  },
  "surfaces": {
    "claude": {
      "broken_symlink": false,
      "disabled_servers": [],
      "effective_servers": [],
      "error": null,
      "extra": [],
      "extra_intentional": [],
      "format": "json",
      "missing": [
        "skillbox"
      ],
      "name": "claude",
      "path": "<FLEET>/repos_real/healthy/.mcp.json",
      "present": false,
      "servers": [],
      "symlink_target": null,
      "unexpected": [],
      "valid": true
    },
    "codex": {
      "broken_symlink": false,
      "disabled_servers": [],
      "effective_servers": [],
      "error": null,
      "extra": [],
      "extra_intentional": [],
      "format": "toml",
      "missing": [
        "skillbox"
      ],
      "name": "codex",
      "path": "<FLEET>/repos_real/healthy/.codex/config.toml",
      "present": false,
      "servers": [],
      "symlink_target": null,
      "unexpected": [],
      "valid": true
    }
  }
}
```

---

## `sbp recalibrate`

**Invocation:** `sbp recalibrate [--cwd <repo>] --json`
**Produced by:** `assemble_recalibrate_payload (issues-only view + fixes[] dry-run previews)`

Machine-actionable cwd recalibration. `sbp recalibrate --json` emits the issues-focused `collect_skill_visibility` core (same fields as `sbp skills --issues-only --format json`) plus a `fixes[]` row per actionable issue. Each fix carries the literal `fix_command`, link dry-run rows, a trimmed `dry_run_preview`, and `packet_on_apply` when linking would return an activation packet. Bare `sbp recalibrate` (no `--json`) still prints the composite human surface (sync/prune dry-runs, beads graph, MCP audit).

### Fields

| Field | Stability | Meaning |
|-------|-----------|---------|
| `cwd` | CONTRACT | Absolute resolved cwd being recalibrated. |
| `matched_scope_rules` | CONTRACT | Rules in force for this cwd. |
| `matched_project_categories` | CONTRACT | Project categories for this cwd. |
| `issues` | CONTRACT | The drift to heal, grouped by kind (the issues-only view's payload). |
| `beads` | CONTRACT | required / required_skills / repo_root / initialized / br / issues. |
| `summary` | CONTRACT | Counters incl. beads_required_skills + beads_issues. |
| `fixes` | CONTRACT | Machine-actionable remediation rows; one per actionable issue. |
| `recommendations` | info | Ranked remediation suggestions. |
| `next_actions` | info | Ordered next commands (dry-run heal moves). |

#### `beads` (beads requirement/readiness)

| Field | Stability | Meaning |
|-------|-----------|---------|
| `required` | CONTRACT | True when an effective skill declares requires_beads. |
| `required_skills` | CONTRACT | Effective skills that require beads (name + path). |
| `repo_root` | CONTRACT | Git repo root for the cwd, or null when none. |
| `beads_dir` | CONTRACT | Path to the repo's .beads dir, or null when none. |
| `initialized` | CONTRACT | Whether a .beads database exists in the repo. |
| `br` | CONTRACT | Whether the `br` CLI is available on PATH. |
| `ok` | CONTRACT | True when the beads requirement is satisfied (or not required). |
| `issues` | CONTRACT | Per-issue {message, hint} for unmet beads requirements. |
| `next_actions` | info | Beads-specific next commands. |

#### `fixes[]` (one machine-actionable remediation row)

| Field | Stability | Meaning |
|-------|-----------|---------|
| `problem` | CONTRACT | Issue kind (e.g. missing_for_cwd, scope_violations). |
| `skill` | CONTRACT | Skill name this fix targets. |
| `command` | CONTRACT | Exact copy-pasteable command that resolves the issue. |
| `links` | CONTRACT | Link actions the dry-run would apply (lifecycle link rows). |
| `dry_run_preview` | CONTRACT | Trimmed skill lifecycle dry-run payload for the fix. |
| `packet_on_apply` | info | activation_packet from the dry-run when the fix links a skill; null otherwise. |

### Example payload

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>` / `<BR_BIN>` / `<REMOTE_ROOT>`.</sub>

```json
{
  "beads": {
    "beads_dir": "<FLEET>/repos_real/overlay-repo/.beads",
    "br": "<BR_BIN>",
    "initialized": false,
    "issues": [],
    "next_actions": [],
    "ok": true,
    "repo_root": "<FLEET>/repos_real/overlay-repo",
    "required": false,
    "required_skills": []
  },
  "cwd": "<FLEET>/repos_real/overlay-repo",
  "fixes": [
    {
      "command": "sbp skill on tiny-ui --cwd $PWD",
      "dry_run_preview": {
        "action": "activate",
        "actions": [
          {
            "blocked_reason": "",
            "category": null,
            "destination": "<FLEET>/repos_real/overlay-repo/.claude/skills/tiny-ui",
            "existing": {
              "state": "missing"
            },
            "op": "link",
            "repo_path": "<FLEET>/repos_real/overlay-repo",
            "root": "<FLEET>/repos_real/overlay-repo/.claude/skills",
            "scope": "project",
            "skill": "tiny-ui",
            "source": "<FLEET>/skills/tiny-ui",
            "source_bucket": "external",
            "status": "would_link",
            "surface": "claude"
          },
          {
            "blocked_reason": "",
            "category": null,
            "destination": "<FLEET>/repos_real/overlay-repo/.codex/skills/tiny-ui",
            "existing": {
              "state": "missing"
            },
            "op": "link",
            "repo_path": "<FLEET>/repos_real/overlay-repo",
            "root": "<FLEET>/repos_real/overlay-repo/.codex/skills",
            "scope": "project",
            "skill": "tiny-ui",
            "source": "<FLEET>/skills/tiny-ui",
            "source_bucket": "external",
            "status": "would_link",
            "surface": "codex"
          }
        ],
        "activation_packet": {
          "instructions": "Use this SKILL.md content immediately in the current agent session. The filesystem links make the skill visible to future Claude and Codex sessions.",
          "name": "tiny-ui",
          "skill_md": "---\nname: tiny-ui\ndescription: Tiny fixture skill tiny-ui.\n---\n\n# tiny-ui\n\nFixture skill body for tiny-ui.\n",
          "skill_md_path": "<FLEET>/skills/tiny-ui/SKILL.md",
          "skill_md_sha256": "953824e6b88a4753851d0309b740812747d507dd780b037e47fdcea540a41e04",
          "source": "<FLEET>/skills/tiny-ui",
          "source_bucket": "external",
          "surface_targets": {
            "claude": [
              "<FLEET>/repos_real/overlay-repo/.claude/skills/tiny-ui"
            ],
            "codex": [
              "<FLEET>/repos_real/overlay-repo/.codex/skills/tiny-ui"
            ]
          }
        },
        "cwd": "<FLEET>/repos_real/overlay-repo",
        "dry_run": true,
        "skill": "tiny-ui",
        "summary": {
          "actions": 2,
          "applied": 0,
          "blocked": 0,
          "link": 2,
          "skipped": 0,
          "unchanged": 0,
          "unlink": 0
        },
        "warnings": []
      },
      "links": [
        {
          "destination": "<FLEET>/repos_real/overlay-repo/.claude/skills/tiny-ui",
          "scope": "project",
          "skill": "tiny-ui",
          "source": "<FLEET>/skills/tiny-ui",
          "status": "would_link",
          "surface": "claude"
        },
        {
          "destination": "<FLEET>/repos_real/overlay-repo/.codex/skills/tiny-ui",
          "scope": "project",
          "skill": "tiny-ui",
          "source": "<FLEET>/skills/tiny-ui",
          "status": "would_link",
          "surface": "codex"
        }
      ],
      "packet_on_apply": {
        "instructions": "Use this SKILL.md content immediately in the current agent session. The filesystem links make the skill visible to future Claude and Codex sessions.",
        "name": "tiny-ui",
        "skill_md": "---\nname: tiny-ui\ndescription: Tiny fixture skill tiny-ui.\n---\n\n# tiny-ui\n\nFixture skill body for tiny-ui.\n",
        "skill_md_path": "<FLEET>/skills/tiny-ui/SKILL.md",
        "skill_md_sha256": "953824e6b88a4753851d0309b740812747d507dd780b037e47fdcea540a41e04",
        "source": "<FLEET>/skills/tiny-ui",
        "source_bucket": "external",
        "surface_targets": {
          "claude": [
            "<FLEET>/repos_real/overlay-repo/.claude/skills/tiny-ui"
          ],
          "codex": [
            "<FLEET>/repos_real/overlay-repo/.codex/skills/tiny-ui"
          ]
        }
      },
      "problem": "missing_for_cwd",
      "skill": "tiny-ui"
    }
  ],
  "issues": {
    "archive_sources": [],
    "broken_global": [],
    "broken_project": [],
    "extra_global": [],
    "global_not_allowed": [],
    "missing_for_cwd": [
      {
        "allowed_paths": [
          "<FLEET>/repos_real/overlay-repo"
        ],
        "categories": [
          "frontend"
        ],
        "fix_command": "sbp skill on tiny-ui --cwd $PWD",
        "name": "tiny-ui",
        "origin": null,
        "policy_path": "<FLEET>/skillbox-config/skill-scope.yaml",
        "reason": "skill is expected for this cwd but is not currently effective",
        "rule_id": "frontend-local",
        "scope_policy_path": "<FLEET>/skillbox-config/skill-scope.yaml",
        "scope_rule": "frontend-local",
        "type": "missing_for_cwd"
      }
    ],
    "scope_violations": [],
    "shadowed": []
  },
  "matched_project_categories": [
    {
      "id": "frontend",
      "match": "<FLEET>/repos_real/overlay-repo",
      "notes": "",
      "paths": [
        "<FLEET>/repos_real/overlay-repo"
      ],
      "policy_path": "<FLEET>/skillbox-config/skill-scope.yaml"
    }
  ],
  "matched_scope_rules": [
    {
      "activation": "",
      "allow_global": false,
      "categories": [
        "frontend"
      ],
      "default": "on",
      "id": "frontend-local",
      "match": "<FLEET>/repos_real/overlay-repo",
      "notes": "",
      "overlay": "",
      "path_match": "prefix",
      "paths": [
        "<FLEET>/repos_real/overlay-repo"
      ],
      "patterns": [
        "tiny-ui"
      ],
      "policy_path": "<FLEET>/skillbox-config/skill-scope.yaml",
      "repos": [],
      "unknown_categories": []
    }
  ],
  "next_actions": [
    "add missing cwd-scoped skills to the active client or project skill-repos.yaml"
  ],
  "recommendations": [
    {
      "action": "add_project_skill",
      "allowed_paths": [
        "<FLEET>/repos_real/overlay-repo"
      ],
      "fix_command": "sbp skill on tiny-ui --cwd $PWD",
      "hint": "Add this skill to the active client's skill-repos.yaml, or durably pin it for this repo with `sbp skill on <skill> --cwd $PWD`. Use `sbp overlay activate <name> --cwd <repo>` for a one-session/cwd policy-evaluated flip, or `sbp overlay on <name>` to PERSIST the overlay across sessions until `overlay off`.",
      "issue_type": "missing_for_cwd",
      "origin": null,
      "policy_path": "<FLEET>/skillbox-config/skill-scope.yaml",
      "rule_id": "frontend-local",
      "scope_rule": "frontend-local",
      "skill": "tiny-ui",
      "target": "project_or_client_skill_repos"
    }
  ],
  "summary": {
    "archive_source_skills": 0,
    "archive_sources": 0,
    "beads_issues": 0,
    "beads_required_skills": 0,
    "broken_by_class": {
      "dangling": 0,
      "moved": 0,
      "other-machine": 0,
      "unreadable": 0
    },
    "broken_global": 0,
    "broken_global_skills": 0,
    "broken_project": 0,
    "broken_project_skills": 0,
    "effective": 1,
    "extra_global": 0,
    "extra_global_skills": 0,
    "global_not_allowed": 0,
    "global_not_allowed_skills": 0,
    "layers": 3,
    "missing_for_cwd": 1,
    "missing_for_cwd_skills": 1,
    "occurrences": 2,
    "parity_divergent": 0,
    "recommendations": 1,
    "scope_violation_skills": 0,
    "scope_violations": 0,
    "shadowed": 0,
    "undeclared_active_overlays": 0,
    "undefined_source_skills": 0,
    "undefined_sources": 0
  }
}
```

---

## `sbp skill why`

**Invocation:** `sbp skill why <skill> [--cwd <repo>] --format json`
**Produced by:** `explain_skill_visibility`

Read-only provenance for ONE skill at ONE cwd, including absence. Same payload shape as `sbp explain` but routed through the `skill why` verb. Walks the precedence spine, names the winning layer (if any), and — when invisible — emits ranked remediation rows with literal `command` strings agents can run without re-deriving policy.

### Fields

| Field | Stability | Meaning |
|-------|-----------|---------|
| `schema_version` | CONTRACT | Versioned tag for the explain payload shape. |
| `skill` | CONTRACT | The skill name explained. |
| `cwd` | CONTRACT | Absolute resolved cwd the provenance is for. |
| `visible` | CONTRACT | True iff the skill resolves to a non-broken effective occurrence here. |
| `reason` | info | Human sentence explaining the verdict. |
| `layer` | CONTRACT | Resolution winner layer id, including disabled/broken winners, or null when none. |
| `winning_layer` | CONTRACT | Canonical winning_layer copied from the same visibility decision that drives the effective set. |
| `layer_family` | CONTRACT | PROJECT|GLOBAL|CLIENT|DEFAULT|OVERRIDE of the resolution winner, or null. |
| `layer_label` | info | Human label for the resolution winner layer, or null. |
| `layer_rank` | CONTRACT | Numeric rank of the resolution winner layer, or null. |
| `winner` | CONTRACT | Trimmed view of the resolution winner (won=true), or null. |
| `layers` | CONTRACT | Ordered provenance trace for this skill; exactly one row has wins=true when a winning layer exists. |
| `occurrences` | CONTRACT | Every occurrence of this skill across layers, each with a won verdict. |
| `lost` | CONTRACT | Non-winning occurrences with a lost_reason. |
| `scope_rules` | CONTRACT | skill-scope.yaml rules naming this skill at this cwd. |
| `inactive_overlay_rules` | CONTRACT | Overlay-gated rules that would apply if the overlay were active. |
| `source_options` | CONTRACT | Discoverable source dirs that could be linked to make it visible. |
| `active_overlays` | CONTRACT | Overlays currently active. |
| `active_clients` | CONTRACT | Active client overlays. |
| `matched_clients` | CONTRACT | Client overlays matching this cwd. |
| `matched_project_categories` | CONTRACT | Project categories for this cwd. |
| `remediation` | CONTRACT | Ranked, narrowest-first paths to visibility, each with kind + exact command. |
| `machine` | CONTRACT | Forward-compatible machine-routing block (always present, may be partial). |
| `registry` | CONTRACT | Forward-compatible {skill_id, registry_ids} block (always present). |
| `next_actions` | info | Commands from remediation, or the already-visible sentinel. |

### Example payload

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>` / `<BR_BIN>` / `<REMOTE_ROOT>`.</sub>

```json
{
  "active_clients": [],
  "active_overlays": [],
  "cwd": "<FLEET>/repos_real/overlay-repo",
  "inactive_overlay_rules": [],
  "layer": null,
  "layer_family": null,
  "layer_label": null,
  "layer_rank": null,
  "layers": [],
  "lost": [],
  "machine": {
    "declared_machines": [
      "devbox-like",
      "mac-like"
    ],
    "machine_id": "devbox-like",
    "resolved": true,
    "source_path": "<FLEET>/skillbox-config/machines.yaml"
  },
  "matched_clients": [],
  "matched_project_categories": [
    {
      "id": "frontend",
      "match": "<FLEET>/repos_real/overlay-repo",
      "notes": "",
      "paths": [
        "<FLEET>/repos_real/overlay-repo"
      ],
      "policy_path": "<FLEET>/skillbox-config/skill-scope.yaml"
    }
  ],
  "next_actions": [
    "sbp skill on needs-beads --cwd $PWD",
    "edit skill-scope.yaml: add a rule with skills:[needs-beads] and a path/category covering <FLEET>/repos_real/overlay-repo"
  ],
  "occurrences": [],
  "reason": "'needs-beads' is NOT visible here, but a source exists and it can be activated",
  "registry": {
    "registry_ids": [],
    "skill_id": null
  },
  "remediation": [
    {
      "command": "sbp skill on needs-beads --cwd $PWD",
      "kind": "on",
      "manage_command": "python3 .env-manager/manage.py skill on needs-beads --cwd <FLEET>/repos_real/overlay-repo",
      "rank": 1,
      "resolved_command": "sbp skill on needs-beads --cwd <FLEET>/repos_real/overlay-repo",
      "why": "a source for 'needs-beads' exists (<FLEET>/private-skills/needs-beads); turning it on pins it for this repo, links it, and returns the SKILL.md packet"
    },
    {
      "command": "edit skill-scope.yaml: add a rule with skills:[needs-beads] and a path/category covering <FLEET>/repos_real/overlay-repo",
      "kind": "rule_edit",
      "policy_files": [
        "<FLEET>/skillbox-config/skill-scope.yaml"
      ],
      "rank": 3,
      "why": "no skill-scope rule currently matches 'needs-beads' for this cwd, so the resolver does not consider it in-scope here"
    }
  ],
  "schema_version": "2026-06-25+skill_explain_layers",
  "scope_rules": [],
  "skill": "needs-beads",
  "source_options": [
    {
      "source": "<FLEET>/private-skills/needs-beads",
      "source_bucket": "external"
    }
  ],
  "visible": false,
  "winner": null,
  "winning_layer": null
}
```

---

## `sbp skill on`

**Invocation:** `sbp skill on <skill> [--cwd <repo>] [--dry-run] --format json`
**Produced by:** `_handle_skill_toggle (on / activate plan + override pin_on)`

Durable repo-local pin ON plus disk links. Writes `pin_on` to `.skillbox/skill-overrides.yaml` (survives recalibrate) and links project skills when needed. Returns an `activation_packet` for immediate session use. `--dry-run` previews override + link actions without writing; a repeat apply is a clean no-op (`noop: true`).

### Fields

| Field | Stability | Meaning |
|-------|-----------|---------|
| `action` | CONTRACT | Verb executed: 'on' or 'off'. |
| `skill` | CONTRACT | Skill name toggled. |
| `cwd` | CONTRACT | Absolute resolved repo cwd the toggle ran against. |
| `requested_to` | CONTRACT | Scope the caller requested (project-only today). |
| `resolved_to` | CONTRACT | Scope the plan resolved to. |
| `categories` | CONTRACT | Project categories targeted when --to category (often empty). |
| `from_scope` | CONTRACT | Installed scope considered for off/unlink (project for skill off). |
| `source_options` | CONTRACT | Resolvable source directories for on/activate. |
| `selected_source` | CONTRACT | Chosen source for on/activate, or null for off. |
| `activation_packet` | CONTRACT | Immediate-use SKILL.md packet on on; null on off. |
| `warnings` | info | Non-fatal plan warnings. |
| `actions` | CONTRACT | Link/unlink rows the toggle would apply; see the action field table. |
| `skipped` | CONTRACT | Skills skipped by the plan (e.g. prune firewall pinned rows). |
| `summary` | CONTRACT | Roll-up counters for planned/applied link+unlink actions. |
| `override` | CONTRACT | Repo override-file mutation preview/result; see the override field table. |
| `changed` | CONTRACT | True when disk and/or override state changed (apply mode). |
| `noop` | CONTRACT | True when neither override nor link actions would change state. |
| `dry_run` | CONTRACT | True when the payload previews without writing. |
| `verification` | info | Optional post-on verify block when --verify is set; null otherwise. |

#### `override` (repo override-file mutation)

| Field | Stability | Meaning |
|-------|-----------|---------|
| `changed` | CONTRACT | True when the override file was written (apply mode). |
| `would_change` | CONTRACT | True when a dry-run would mutate the override file. |
| `policy_path` | CONTRACT | Absolute path of .skillbox/skill-overrides.yaml. |
| `pin` | CONTRACT | Override list touched: pin_on or pin_off. |

#### `activation_packet` (immediate SKILL.md packet)

| Field | Stability | Meaning |
|-------|-----------|---------|
| `name` | CONTRACT | Skill name in the activation packet. |
| `source` | CONTRACT | Resolved source directory backing the skill. |
| `source_bucket` | CONTRACT | Source bucket id (external/private/etc.). |
| `skill_md_path` | CONTRACT | Absolute path to SKILL.md used for the packet. |
| `skill_md_sha256` | CONTRACT | SHA-256 of SKILL.md for verify consumers. |
| `skill_md` | CONTRACT | Full SKILL.md body for immediate session use. |
| `surface_targets` | CONTRACT | Per-surface link destinations the packet covers. |
| `instructions` | info | Human guidance for using the packet in-session. |

#### `actions[]` (one lifecycle link/unlink row)

| Field | Stability | Meaning |
|-------|-----------|---------|
| `op` | CONTRACT | Lifecycle op: link or unlink. |
| `skill` | CONTRACT | Skill name for this action row. |
| `source` | CONTRACT | Source path for link rows; prior target for unlink rows. |
| `source_bucket` | info | Source bucket for link rows. |
| `destination` | CONTRACT | Installed symlink path affected. |
| `root` | CONTRACT | Skills root directory under the repo for link rows. |
| `scope` | CONTRACT | project or global scope of the action. |
| `surface` | CONTRACT | claude or codex surface. |
| `category` | info | Project category when scoped by category. |
| `repo_path` | CONTRACT | Repo root owning the destination. |
| `existing` | CONTRACT | Prior install state at the destination. |
| `blocked_reason` | CONTRACT | Empty when allowed; otherwise why the row is blocked. |
| `status` | CONTRACT | Dry-run/applied status (would_link, would_unlink, linked, ...). |

#### `summary` (action counters)

| Field | Stability | Meaning |
|-------|-----------|---------|
| `actions` | CONTRACT | Total planned/applied action rows. |
| `link` | CONTRACT | Link action count. |
| `unlink` | CONTRACT | Unlink action count. |
| `blocked` | CONTRACT | Blocked action count. |
| `skipped` | CONTRACT | Skipped action count. |
| `applied` | CONTRACT | Applied action count (apply mode). |
| `unchanged` | CONTRACT | Actions that left destination unchanged. |

### Example payload

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>` / `<BR_BIN>` / `<REMOTE_ROOT>`.</sub>

```json
{
  "action": "on",
  "actions": [
    {
      "blocked_reason": "",
      "category": null,
      "destination": "<FLEET>/repos_real/overlay-repo/.claude/skills/tiny-ui",
      "existing": {
        "state": "missing"
      },
      "op": "link",
      "repo_path": "<FLEET>/repos_real/overlay-repo",
      "root": "<FLEET>/repos_real/overlay-repo/.claude/skills",
      "scope": "project",
      "skill": "tiny-ui",
      "source": "<FLEET>/skills/tiny-ui",
      "source_bucket": "external",
      "status": "would_link",
      "surface": "claude"
    },
    {
      "blocked_reason": "",
      "category": null,
      "destination": "<FLEET>/repos_real/overlay-repo/.codex/skills/tiny-ui",
      "existing": {
        "state": "missing"
      },
      "op": "link",
      "repo_path": "<FLEET>/repos_real/overlay-repo",
      "root": "<FLEET>/repos_real/overlay-repo/.codex/skills",
      "scope": "project",
      "skill": "tiny-ui",
      "source": "<FLEET>/skills/tiny-ui",
      "source_bucket": "external",
      "status": "would_link",
      "surface": "codex"
    }
  ],
  "activation_packet": {
    "instructions": "Use this SKILL.md content immediately in the current agent session. The filesystem links make the skill visible to future Claude and Codex sessions.",
    "name": "tiny-ui",
    "skill_md": "---\nname: tiny-ui\ndescription: Tiny fixture skill tiny-ui.\n---\n\n# tiny-ui\n\nFixture skill body for tiny-ui.\n",
    "skill_md_path": "<FLEET>/skills/tiny-ui/SKILL.md",
    "skill_md_sha256": "953824e6b88a4753851d0309b740812747d507dd780b037e47fdcea540a41e04",
    "source": "<FLEET>/skills/tiny-ui",
    "source_bucket": "external",
    "surface_targets": {
      "claude": [
        "<FLEET>/repos_real/overlay-repo/.claude/skills/tiny-ui"
      ],
      "codex": [
        "<FLEET>/repos_real/overlay-repo/.codex/skills/tiny-ui"
      ]
    }
  },
  "categories": [],
  "changed": false,
  "cwd": "<FLEET>/repos_real/overlay-repo",
  "dry_run": true,
  "from_scope": "all",
  "noop": false,
  "override": {
    "changed": false,
    "pin": "pin_on",
    "policy_path": "<FLEET>/repos_real/overlay-repo/.skillbox/skill-overrides.yaml",
    "would_change": true
  },
  "requested_to": "project",
  "resolved_to": "project",
  "selected_source": {
    "explicit": false,
    "name": "tiny-ui",
    "root": "<FLEET>/skills",
    "source": "<FLEET>/skills/tiny-ui",
    "source_bucket": "external"
  },
  "skill": "tiny-ui",
  "skipped": [],
  "source_options": [
    {
      "explicit": false,
      "name": "tiny-ui",
      "root": "<FLEET>/skills",
      "source": "<FLEET>/skills/tiny-ui",
      "source_bucket": "external"
    }
  ],
  "summary": {
    "actions": 2,
    "applied": 0,
    "blocked": 0,
    "link": 2,
    "skipped": 0,
    "unchanged": 0,
    "unlink": 0
  },
  "verification": null,
  "warnings": []
}
```

---

## `sbp skill off`

**Invocation:** `sbp skill off <skill> [--cwd <repo>] [--dry-run] --format json`
**Produced by:** `_handle_skill_toggle (off / prune plan + override pin_off)`

Durable repo-local pin OFF plus project unlink. Writes `pin_off` to `.skillbox/skill-overrides.yaml` and unlinks project installs. Refuses floor skills (smart/sbp). `--dry-run` previews override + unlink rows; `activation_packet` is always null.

### Fields

| Field | Stability | Meaning |
|-------|-----------|---------|
| `action` | CONTRACT | Verb executed: 'on' or 'off'. |
| `skill` | CONTRACT | Skill name toggled. |
| `cwd` | CONTRACT | Absolute resolved repo cwd the toggle ran against. |
| `requested_to` | CONTRACT | Scope the caller requested (project-only today). |
| `resolved_to` | CONTRACT | Scope the plan resolved to. |
| `categories` | CONTRACT | Project categories targeted when --to category (often empty). |
| `from_scope` | CONTRACT | Installed scope considered for off/unlink (project for skill off). |
| `source_options` | CONTRACT | Resolvable source directories for on/activate. |
| `selected_source` | CONTRACT | Chosen source for on/activate, or null for off. |
| `activation_packet` | CONTRACT | Immediate-use SKILL.md packet on on; null on off. |
| `warnings` | info | Non-fatal plan warnings. |
| `actions` | CONTRACT | Link/unlink rows the toggle would apply; see the action field table. |
| `skipped` | CONTRACT | Skills skipped by the plan (e.g. prune firewall pinned rows). |
| `summary` | CONTRACT | Roll-up counters for planned/applied link+unlink actions. |
| `override` | CONTRACT | Repo override-file mutation preview/result; see the override field table. |
| `changed` | CONTRACT | True when disk and/or override state changed (apply mode). |
| `noop` | CONTRACT | True when neither override nor link actions would change state. |
| `dry_run` | CONTRACT | True when the payload previews without writing. |
| `verification` | info | Optional post-on verify block when --verify is set; null otherwise. |

#### `override` (repo override-file mutation)

| Field | Stability | Meaning |
|-------|-----------|---------|
| `changed` | CONTRACT | True when the override file was written (apply mode). |
| `would_change` | CONTRACT | True when a dry-run would mutate the override file. |
| `policy_path` | CONTRACT | Absolute path of .skillbox/skill-overrides.yaml. |
| `pin` | CONTRACT | Override list touched: pin_on or pin_off. |

#### `actions[]` (one lifecycle unlink row)

| Field | Stability | Meaning |
|-------|-----------|---------|
| `op` | CONTRACT | Lifecycle op: link or unlink. |
| `skill` | CONTRACT | Skill name for this action row. |
| `destination` | CONTRACT | Installed symlink path affected. |
| `scope` | CONTRACT | project or global scope of the action. |
| `surface` | CONTRACT | claude or codex surface. |
| `source` | CONTRACT | Source path for link rows; prior target for unlink rows. |
| `layer` | info | Layer id for unlink rows derived from visibility. |
| `reason` | info | Human reason for unlink (e.g. pin_off, prune). |
| `existing` | CONTRACT | Prior install state at the destination. |
| `status` | CONTRACT | Dry-run/applied status (would_link, would_unlink, linked, ...). |

#### `summary` (action counters)

| Field | Stability | Meaning |
|-------|-----------|---------|
| `actions` | CONTRACT | Total planned/applied action rows. |
| `link` | CONTRACT | Link action count. |
| `unlink` | CONTRACT | Unlink action count. |
| `blocked` | CONTRACT | Blocked action count. |
| `skipped` | CONTRACT | Skipped action count. |
| `applied` | CONTRACT | Applied action count (apply mode). |
| `unchanged` | CONTRACT | Actions that left destination unchanged. |

### Example payload

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>` / `<BR_BIN>` / `<REMOTE_ROOT>`.</sub>

```json
{
  "action": "off",
  "actions": [
    {
      "destination": "<FLEET>/repos_real/overlay-repo/.claude/skills/tiny-marketing",
      "existing": {
        "link_target": "<FLEET>/private-skills/tiny-marketing",
        "resolved": "<FLEET>/private-skills/tiny-marketing",
        "state": "different_link"
      },
      "layer": "project:claude:<FLEET>/repos_real/overlay-repo",
      "op": "unlink",
      "reason": "pin_off",
      "scope": "project",
      "skill": "tiny-marketing",
      "source": "<FLEET>/private-skills/tiny-marketing",
      "status": "would_unlink",
      "surface": "claude"
    }
  ],
  "activation_packet": null,
  "categories": [],
  "changed": false,
  "cwd": "<FLEET>/repos_real/overlay-repo",
  "dry_run": true,
  "from_scope": "project",
  "noop": false,
  "override": {
    "changed": false,
    "pin": "pin_off",
    "policy_path": "<FLEET>/repos_real/overlay-repo/.skillbox/skill-overrides.yaml",
    "would_change": true
  },
  "requested_to": "project",
  "resolved_to": "project",
  "selected_source": null,
  "skill": "tiny-marketing",
  "skipped": [],
  "source_options": [],
  "summary": {
    "actions": 1,
    "applied": 0,
    "blocked": 0,
    "link": 0,
    "skipped": 0,
    "unchanged": 0,
    "unlink": 1
  },
  "verification": null,
  "warnings": []
}
```

---

## `sbp skill togglable`

**Invocation:** `sbp skill togglable [--cwd <repo>] --format json`
**Produced by:** `build_skill_togglable_payload`

Write-affordance switchboard for one cwd: every skill the policy marks as flippable here, its current state (`on`, `off`, `missing_for_cwd`, `pinned_on`, `pinned_off`), who pinned it (`override` vs `policy`), and the literal `command_to_flip` to transition state. Distinct from `sbp skills` (visibility/read).

### Fields

| Field | Stability | Meaning |
|-------|-----------|---------|
| `cwd` | CONTRACT | Absolute resolved cwd the switchboard was computed for. |
| `items` | CONTRACT | Every flippable skill at this cwd; see the item field table. |

#### `items[]` (one flippable skill row)

| Field | Stability | Meaning |
|-------|-----------|---------|
| `skill` | CONTRACT | Skill name. |
| `state` | CONTRACT | on | off | missing_for_cwd | pinned_on | pinned_off. |
| `source` | CONTRACT | Installed path when on; null when absent. |
| `pinned_by` | CONTRACT | override when repo override lists drive state; else policy. |
| `command_to_flip` | CONTRACT | Literal sbp skill on/off command to transition state. |

### Example payload

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>` / `<BR_BIN>` / `<REMOTE_ROOT>`.</sub>

```json
{
  "cwd": "<FLEET>/repos_real/overlay-repo",
  "items": [
    {
      "command_to_flip": "sbp skill on tiny-cli --cwd <FLEET>/repos_real/overlay-repo",
      "pinned_by": "override",
      "skill": "tiny-cli",
      "source": null,
      "state": "pinned_off"
    },
    {
      "command_to_flip": "sbp skill off tiny-marketing --cwd <FLEET>/repos_real/overlay-repo",
      "pinned_by": "policy",
      "skill": "tiny-marketing",
      "source": "<FLEET>/repos_real/overlay-repo/.claude/skills/tiny-marketing",
      "state": "on"
    },
    {
      "command_to_flip": "sbp skill on tiny-ui --cwd <FLEET>/repos_real/overlay-repo",
      "pinned_by": "policy",
      "skill": "tiny-ui",
      "source": null,
      "state": "missing_for_cwd"
    }
  ]
}
```

---

## `sbp explain`

**Invocation:** `sbp explain <skill> [--cwd <repo>] --format json`
**Produced by:** `explain_skill_visibility`

Full provenance for ONE skill at ONE cwd: is it visible, via which layer, which occurrences lost and why, and — when invisible — the ranked, narrowest path to visibility with the EXACT command for each option. `machine` and `registry` are forward-compatible blocks: always present (possibly partial) so routing/registry consumers can grow without a schema break.

### Fields

| Field | Stability | Meaning |
|-------|-----------|---------|
| `schema_version` | CONTRACT | Versioned tag for the explain payload shape. |
| `skill` | CONTRACT | The skill name explained. |
| `cwd` | CONTRACT | Absolute resolved cwd the provenance is for. |
| `visible` | CONTRACT | True iff the skill resolves to a non-broken effective occurrence here. |
| `reason` | info | Human sentence explaining the verdict. |
| `layer` | CONTRACT | Resolution winner layer id, including disabled/broken winners, or null when none. |
| `winning_layer` | CONTRACT | Canonical winning_layer copied from the same visibility decision that drives the effective set. |
| `layer_family` | CONTRACT | PROJECT|GLOBAL|CLIENT|DEFAULT|OVERRIDE of the resolution winner, or null. |
| `layer_label` | info | Human label for the resolution winner layer, or null. |
| `layer_rank` | CONTRACT | Numeric rank of the resolution winner layer, or null. |
| `winner` | CONTRACT | Trimmed view of the resolution winner (won=true), or null. |
| `layers` | CONTRACT | Ordered provenance trace for this skill; exactly one row has wins=true when a winning layer exists. |
| `occurrences` | CONTRACT | Every occurrence of this skill across layers, each with a won verdict. |
| `lost` | CONTRACT | Non-winning occurrences with a lost_reason. |
| `scope_rules` | CONTRACT | skill-scope.yaml rules naming this skill at this cwd. |
| `inactive_overlay_rules` | CONTRACT | Overlay-gated rules that would apply if the overlay were active. |
| `source_options` | CONTRACT | Discoverable source dirs that could be linked to make it visible. |
| `active_overlays` | CONTRACT | Overlays currently active. |
| `active_clients` | CONTRACT | Active client overlays. |
| `matched_clients` | CONTRACT | Client overlays matching this cwd. |
| `matched_project_categories` | CONTRACT | Project categories for this cwd. |
| `remediation` | CONTRACT | Ranked, narrowest-first paths to visibility, each with kind + exact command. |
| `machine` | CONTRACT | Forward-compatible machine-routing block (always present, may be partial). |
| `registry` | CONTRACT | Forward-compatible {skill_id, registry_ids} block (always present). |
| `next_actions` | info | Commands from remediation, or the already-visible sentinel. |

### Example payload

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>` / `<BR_BIN>` / `<REMOTE_ROOT>`.</sub>

```json
{
  "active_clients": [],
  "active_overlays": [
    "marketing"
  ],
  "cwd": "<FLEET>/repos_real/healthy",
  "inactive_overlay_rules": [],
  "layer": "repo-override-file",
  "layer_family": "OVERRIDE",
  "layer_label": "repo override file",
  "layer_rank": 60,
  "layers": [
    {
      "availability": "override",
      "layer": "repo-override-file",
      "layer_family": "OVERRIDE",
      "layer_label": "repo override file",
      "layer_rank": 60,
      "override_action": "pin_on",
      "path": "<FLEET>/repos_real/healthy",
      "policy_path": "<FLEET>/repos_real/healthy/.skillbox/skill-overrides.yaml",
      "source": "<FLEET>/private-skills/needs-beads",
      "source_bucket": "external",
      "state": "pinned",
      "wins": true,
      "won": true
    }
  ],
  "lost": [],
  "machine": {
    "declared_machines": [
      "devbox-like",
      "mac-like"
    ],
    "machine_id": "devbox-like",
    "resolved": true,
    "source_path": "<FLEET>/skillbox-config/machines.yaml"
  },
  "matched_clients": [],
  "matched_project_categories": [
    {
      "id": "cli",
      "match": "<FLEET>/repos_real/healthy",
      "notes": "",
      "paths": [
        "<FLEET>/repos_real/healthy"
      ],
      "policy_path": "<FLEET>/skillbox-config/skill-scope.yaml"
    }
  ],
  "next_actions": [
    "already visible; no action needed"
  ],
  "occurrences": [
    {
      "availability": "override",
      "layer": "repo-override-file",
      "layer_family": "OVERRIDE",
      "layer_label": "repo override file",
      "layer_rank": 60,
      "override_action": "pin_on",
      "path": "<FLEET>/repos_real/healthy",
      "policy_path": "<FLEET>/repos_real/healthy/.skillbox/skill-overrides.yaml",
      "source": "<FLEET>/private-skills/needs-beads",
      "source_bucket": "external",
      "state": "pinned",
      "wins": true,
      "won": true
    }
  ],
  "reason": "'needs-beads' IS visible at cwd via the OVERRIDE layer (repo-override-file)",
  "registry": {
    "registry_ids": [],
    "skill_id": null
  },
  "remediation": [],
  "schema_version": "2026-06-25+skill_explain_layers",
  "scope_rules": [],
  "skill": "needs-beads",
  "source_options": [
    {
      "source": "<FLEET>/private-skills/needs-beads",
      "source_bucket": "external"
    }
  ],
  "visible": true,
  "winner": {
    "availability": "override",
    "layer": "repo-override-file",
    "layer_family": "OVERRIDE",
    "layer_label": "repo override file",
    "layer_rank": 60,
    "override_action": "pin_on",
    "path": "<FLEET>/repos_real/healthy",
    "policy_path": "<FLEET>/repos_real/healthy/.skillbox/skill-overrides.yaml",
    "source": "<FLEET>/private-skills/needs-beads",
    "source_bucket": "external",
    "state": "pinned",
    "wins": true,
    "won": true
  },
  "winning_layer": "repo-override-file"
}
```

---

## `sbp doctor`

**Invocation:** `sbp doctor [--cwd <repo>] --format json` (a.k.a. structure-doctor)
**Produced by:** `run_structure_doctor`

The structural verification front door. Runs every gate read-only and returns `{ok, gates, summary, exit_code}`. FAIL is the only status that flips `exit_code`; INCO (e.g. a dependency unreachable) and PASS both exit 0 — INCO is never a regression. The example below uses canned gate outcomes (one of each status) for determinism; `duration_s` is real wall-clock in production (normalized to 0.0 here).

### Fields

| Field | Stability | Meaning |
|-------|-----------|---------|
| `ok` | CONTRACT | True iff no gate is FAIL (INCO and PASS are both ok). |
| `config_root` | CONTRACT | Resolved skillbox-config root, or null when not found (gates go INCO). |
| `runtime_root` | CONTRACT | Resolved runtime repo root. |
| `cwd` | CONTRACT | Absolute resolved cwd the gates ran against. |
| `gates` | CONTRACT | One row per gate in declaration order; see the gate field table. |
| `summary` | CONTRACT | Gate counters + structure budget; structure_within_budget guards the <60s promise. |
| `exit_code` | CONTRACT | 1 iff any gate FAILed, else 0 (INCO never flips it). |

#### `gates[]` (one gate outcome)

| Field | Stability | Meaning |
|-------|-----------|---------|
| `name` | CONTRACT | Gate id (e.g. structure_invariants, mcp_parity, runtime_doctor). |
| `kind` | CONTRACT | 'structure' or 'runtime'; only structure gates count toward the budget. |
| `status` | CONTRACT | PASS | FAIL | INCO. Only FAIL flips exit_code; INCO is never a regression. |
| `duration_s` | info | Wall-clock the gate took (non-deterministic; normalized in this example). |
| `fix_command` | CONTRACT | Exact command to remediate this gate when not PASS. |
| `detail` | info | Human one-line outcome detail. |

### Example payload

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>` / `<BR_BIN>` / `<REMOTE_ROOT>`.</sub>

```json
{
  "config_root": null,
  "cwd": "<RUNTIME_ROOT>/sample-repo",
  "exit_code": 1,
  "gates": [
    {
      "detail": "12 invariant(s) passed",
      "duration_s": 0.0,
      "fix_command": "<fix for structure_invariants>",
      "kind": "structure",
      "name": "structure_invariants",
      "status": "PASS"
    },
    {
      "detail": "claude/codex MCP drift: foo only in claude",
      "duration_s": 0.0,
      "fix_command": "<fix for mcp_parity>",
      "kind": "structure",
      "name": "mcp_parity",
      "status": "FAIL"
    },
    {
      "detail": "skillbox-config repo not found on this box",
      "duration_s": 0.0,
      "fix_command": "<fix for skill_drift>",
      "kind": "structure",
      "name": "skill_drift",
      "status": "INCO"
    },
    {
      "detail": "make doctor: all checks pass",
      "duration_s": 0.0,
      "fix_command": "<fix for runtime_doctor>",
      "kind": "runtime",
      "name": "runtime_doctor",
      "status": "PASS"
    }
  ],
  "ok": false,
  "runtime_root": "<RUNTIME_ROOT>",
  "summary": {
    "fail": 1,
    "inco": 1,
    "pass": 2,
    "runtime_duration_s": 0.0,
    "structure_budget_s": 60.0,
    "structure_duration_s": 0.0,
    "structure_within_budget": true,
    "total": 4
  }
}
```

---

## `fleet converge`

**Invocation:** `sbp fleet converge [--cwd <repo>] [--all] [--no-mcp] --format json`
**Produced by:** `build_fleet_converge_plan`

ONE diffable, PLAN-ONLY heal plan across the deduped canonical fleet (the same candidate set `collect_skill_audit` reports). Each repo's drift is grouped into the five fixed triage classes (`relink`, `prune`, `sync`, `policy`, `mcp`); every action carries its exact single-repo command. `dry_run` is always true — converge never writes.

### Fields

| Field | Stability | Meaning |
|-------|-----------|---------|
| `kind` | CONTRACT | Literal 'fleet-converge-plan' discriminator. |
| `dry_run` | CONTRACT | Always true — converge is PLAN ONLY and never writes. |
| `cwd` | CONTRACT | Absolute resolved cwd the plan was invoked from. |
| `scan_roots` | CONTRACT | Roots walked to build the candidate fleet. |
| `max_depth` | CONTRACT | Max repo-discovery depth under each scan root. |
| `classes` | CONTRACT | The five triage classes in fixed order: relink, prune, sync, policy, mcp. |
| `summary` | CONTRACT | Fleet roll-up; by_class is keyed by the five classes; actions_total is their sum. |
| `repos` | CONTRACT | Per-repo heal plans (sorted by path); see the repo-row field table. |
| `next_actions` | info | One representative bulk command per non-empty class. |

#### `repos[]` (one per-repo heal plan)

| Field | Stability | Meaning |
|-------|-----------|---------|
| `path` | CONTRACT | Absolute canonical repo path. |
| `sources` | CONTRACT | Why this repo is in the fleet (scan_root / category / client provenance). |
| `state` | CONTRACT | 'ok' or 'missing' (a declared path absent on this box; carries no actions). |
| `matched_scope_rules` | CONTRACT | Rule ids in force for the repo (absent on missing repos). |
| `categories` | CONTRACT | Project categories the repo falls under (absent on missing repos). |
| `actions` | CONTRACT | Heal actions grouped by the five classes; each action carries its exact command. |
| `counts` | CONTRACT | Action count per class for this repo. |
| `total` | CONTRACT | Sum of counts across classes for this repo. |

### Example payload

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>` / `<BR_BIN>` / `<REMOTE_ROOT>`.</sub>

```json
{
  "classes": [
    "relink",
    "prune",
    "sync",
    "policy",
    "mcp"
  ],
  "cwd": "<FLEET>/repos_real/overlay-repo",
  "dry_run": true,
  "kind": "fleet-converge-plan",
  "max_depth": 3,
  "next_actions": [
    "[relink] ln -sfn <FLEET>/skills/tiny-ui <FLEET>/repos_real/other-machine/.claude/skills/tiny-ui",
    "[prune] rm <FLEET>/repos_real/dangling/.claude/skills/ghost  # prune dead link 'ghost'",
    "[sync] manage.py skill sync tiny-ui --cwd <FLEET>/repos_real/overlay-repo --dry-run",
    "[policy] manage.py skill prune --cwd <FLEET>/repos_real/other-machine --from project --dry-run"
  ],
  "repos": [
    {
      "actions": {
        "mcp": [],
        "policy": [],
        "prune": [
          {
            "class": "prune",
            "command": "rm <FLEET>/repos_real/dangling/.claude/skills/ghost  # prune dead link 'ghost'",
            "link_target": "<FLEET>/deleted-source",
            "origin": "dangling",
            "path": "<FLEET>/repos_real/dangling/.claude/skills/ghost",
            "skill": "ghost",
            "suggested_action": "prune"
          }
        ],
        "relink": [],
        "sync": []
      },
      "categories": [],
      "counts": {
        "mcp": 0,
        "policy": 0,
        "prune": 1,
        "relink": 0,
        "sync": 0
      },
      "matched_scope_rules": [],
      "path": "<FLEET>/repos_real/dangling",
      "sources": [
        "scan_root:<FLEET>/repos_real"
      ],
      "state": "ok",
      "total": 1
    },
    {
      "actions": {
        "mcp": [],
        "policy": [
          {
            "allowed_paths": [
              "<FLEET>/repos_real/overlay-repo"
            ],
            "class": "policy",
            "command": "manage.py skill prune --cwd <FLEET>/repos_real/other-machine --from project --dry-run",
            "path": "<FLEET>/repos_real/other-machine/.claude/skills/tiny-ui",
            "policy_edit": "edit rule 'frontend-local' in <FLEET>/skillbox-config/skill-scope.yaml to allow 'tiny-ui' at <FLEET>/repos_real/other-machine",
            "reason": "installed outside allowed repo path",
            "scope_policy_path": "<FLEET>/skillbox-config/skill-scope.yaml",
            "scope_rule": "frontend-local",
            "skill": "tiny-ui"
          }
        ],
        "prune": [],
        "relink": [
          {
            "class": "relink",
            "command": "ln -sfn <FLEET>/skills/tiny-ui <FLEET>/repos_real/other-machine/.claude/skills/tiny-ui",
            "link_target": "<REMOTE_ROOT>/skills/tiny-ui",
            "origin": "moved",
            "path": "<FLEET>/repos_real/other-machine/.claude/skills/tiny-ui",
            "skill": "tiny-ui",
            "suggested_action": "relink"
          }
        ],
        "sync": []
      },
      "categories": [],
      "counts": {
        "mcp": 0,
        "policy": 1,
        "prune": 0,
        "relink": 1,
        "sync": 0
      },
      "matched_scope_rules": [],
      "path": "<FLEET>/repos_real/other-machine",
      "sources": [
        "scan_root:<FLEET>/repos_real"
      ],
      "state": "ok",
      "total": 2
    },
    {
      "actions": {
        "mcp": [],
        "policy": [],
        "prune": [],
        "relink": [],
        "sync": [
          {
            "class": "sync",
            "command": "manage.py skill sync tiny-ui --cwd <FLEET>/repos_real/overlay-repo --dry-run",
            "reason": "skill is expected for this cwd but is not currently effective",
            "scope_policy_path": "<FLEET>/skillbox-config/skill-scope.yaml",
            "scope_rule": "frontend-local",
            "skill": "tiny-ui"
          }
        ]
      },
      "categories": [
        "frontend"
      ],
      "counts": {
        "mcp": 0,
        "policy": 0,
        "prune": 0,
        "relink": 0,
        "sync": 1
      },
      "matched_scope_rules": [
        "frontend-local"
      ],
      "path": "<FLEET>/repos_real/overlay-repo",
      "sources": [
        "category:frontend",
        "scan_root:<FLEET>/repos_real"
      ],
      "state": "ok",
      "total": 1
    }
  ],
  "scan_roots": [
    "<FLEET>/repos_real"
  ],
  "summary": {
    "actions_total": 4,
    "by_class": {
      "mcp": 0,
      "policy": 1,
      "prune": 1,
      "relink": 1,
      "sync": 1
    },
    "candidate_repos": 4,
    "missing_repos": 0,
    "reported_repos": 3,
    "repos_with_plan": 3
  }
}
```
