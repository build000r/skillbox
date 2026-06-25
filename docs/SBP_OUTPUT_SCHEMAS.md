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

- [`sbp skills`](#sbp-skills)
- [`sbp candidates`](#sbp-candidates)
- [`sbp mcp`](#sbp-mcp)
- [`sbp recalibrate`](#sbp-recalibrate)
- [`sbp explain`](#sbp-explain)
- [`sbp doctor`](#sbp-doctor)
- [`fleet converge`](#fleet-converge)

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

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>`.</sub>

```json
{
  "active_clients": [],
  "active_profiles": [
    "core"
  ],
  "beads": {
    "beads_dir": "<FLEET>/repos_real/overlay-repo/.beads",
    "br": "/home/skillbox/.local/bin/br",
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
        "fix_command": "sbp skill activate tiny-ui --cwd <repo>",
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
      "fix_command": "sbp skill activate tiny-ui --cwd <repo>",
      "hint": "Add this skill to the active client's skill-repos.yaml, or activate it for this cwd ephemerally with `sbp skill activate <skill> --cwd <repo>`. Use `sbp overlay activate <name> --cwd <repo>` for a one-session/cwd policy-evaluated flip, or `sbp overlay on <name>` to PERSIST the overlay across sessions until `overlay off`.",
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

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>`.</sub>

```json
{
  "active_clients": [],
  "active_profiles": [
    "core"
  ],
  "beads": {
    "beads_dir": "<FLEET>/repos_real/healthy/.beads",
    "br": "/home/skillbox/.local/bin/br",
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
        "source": "<FLEET>/repos_real/healthy/.skillbox/skill-overrides.yaml"
      },
      {
        "enabled": false,
        "layer": "repo-override-file",
        "layer_label": "repo override file",
        "layer_rank": 60,
        "name": "swarm",
        "source": "<FLEET>/repos_real/healthy/.skillbox/skill-overrides.yaml"
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

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>`.</sub>

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

**Invocation:** `sbp recalibrate [--cwd <repo>]` (composite; machine core shown below)  
**Produced by:** `collect_skill_visibility (issues-only view) + embedded beads block`

A COMPOSITE human surface that stitches together several dry-run sub-calls (`sbp skills --issues-only`, `sbp skill sync --dry-run`, `sbp skill prune --dry-run`, the beads graph, and `sbp mcp`). Its single machine-readable core is the issues-focused `collect_skill_visibility` payload below — same shape as `sbp skills --issues-only --format json`, whose `beads` block the wrapper parses directly.

### Fields

| Field | Stability | Meaning |
|-------|-----------|---------|
| `cwd` | CONTRACT | Absolute resolved cwd being recalibrated. |
| `matched_scope_rules` | CONTRACT | Rules in force for this cwd. |
| `matched_project_categories` | CONTRACT | Project categories for this cwd. |
| `issues` | CONTRACT | The drift to heal, grouped by kind (the issues-only view's payload). |
| `beads` | CONTRACT | required / required_skills / repo_root / initialized / br / issues. |
| `summary` | CONTRACT | Counters incl. beads_required_skills + beads_issues. |
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

### Example payload

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>`.</sub>

```json
{
  "beads": {
    "beads_dir": "<FLEET>/repos_real/overlay-repo/.beads",
    "br": "/home/skillbox/.local/bin/br",
    "initialized": false,
    "issues": [],
    "next_actions": [],
    "ok": true,
    "repo_root": "<FLEET>/repos_real/overlay-repo",
    "required": false,
    "required_skills": []
  },
  "cwd": "<FLEET>/repos_real/overlay-repo",
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
        "fix_command": "sbp skill activate tiny-ui --cwd <repo>",
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
      "fix_command": "sbp skill activate tiny-ui --cwd <repo>",
      "hint": "Add this skill to the active client's skill-repos.yaml, or activate it for this cwd ephemerally with `sbp skill activate <skill> --cwd <repo>`. Use `sbp overlay activate <name> --cwd <repo>` for a one-session/cwd policy-evaluated flip, or `sbp overlay on <name>` to PERSIST the overlay across sessions until `overlay off`.",
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

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>`.</sub>

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

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>`.</sub>

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

<sub>From the `tests/fixture_fleet.py` estate; absolute paths normalized to `<FLEET>` / `<RUNTIME_ROOT>`.</sub>

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
            "link_target": "<FLEET>/fake-mac-root/skills/tiny-ui",
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
