<!-- GENERATED FILE: do not hand-edit. -->
<!-- Regenerate: python3 .env-manager/manage.py registry-docs --write. -->
<!-- Generated from command registry ABI: 2026-06-11+agent_ops_brain. -->

# Skillbox API Reference

Generated from command registry ABI `2026-06-11+agent_ops_brain`.
Do not edit by hand; run `python3 .env-manager/manage.py registry-docs --write`.

Registry entries: 42.

## Tier 1

### brain

#### brain.explain

Explain one graph node - command, check, service, bead, skill, or MCP tool - with evidence.

- Surfaces: `cli`, `mcp`
- Scopes: None
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: `skillbox_explain`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `format` | `enum[json\|text]?` | no |
| `target` | `string` | yes |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `evidence` | `object[]` | yes |
| `incoming` | `string[]` | yes |
| `next_actions` | `object[]` | yes |
| `node` | `object` | yes |
| `ok` | `boolean` | yes |
| `outgoing` | `string[]` | yes |
| `summary` | `string` | yes |

**Examples**

```bash
python3 .env-manager/manage.py explain check:runtime-doctor --format json
```

**Validation**

```bash
python3 -m unittest tests.test_agent_ops_command_registry
```

**Graph Nodes**: `check`, `service`, `bead`, `skill`, `mcp_tool`, `command`, `evidence`

#### brain.graph

Expose the typed runtime graph and algorithm outputs such as cycles and critical path.

- Surfaces: `cli`, `mcp`
- Scopes: `client`, `profile`
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: `skillbox_graph`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `algorithm` | `enum[topology\|cycles\|scc\|critical-path\|blast-radius\|min-unblock\|shortest-path\|all]?` | no |
| `blocked-node` | `string?` | no |
| `client` | `string?` | no |
| `format` | `enum[json\|text]?` | no |
| `node` | `string?` | no |
| `source` | `string?` | no |
| `target` | `string?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `edges` | `object[]` | yes |
| `nodes` | `object[]` | yes |
| `ok` | `boolean` | yes |
| `result` | `object` | yes |

**Examples**

```bash
python3 .env-manager/manage.py graph --algorithm critical-path --format json
```

**Validation**

```bash
python3 -m unittest tests.test_agent_ops_command_registry
```

**Graph Nodes**: `client`, `profile`, `repo`, `service`, `task`, `check`, `skill`, `mcp_tool`, `command`, `bead`

#### brain.next

Rank the highest-leverage next actions with reasons, claim commands, and validations.

- Surfaces: `cli`, `mcp`
- Scopes: `client`, `profile`
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: `skillbox_next`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `client` | `string?` | no |
| `format` | `enum[json\|text]?` | no |
| `limit` | `integer?` | no |
| `profile` | `string[]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `blockers` | `object[]` | yes |
| `context` | `object` | yes |
| `ok` | `boolean` | yes |
| `recommendations` | `object[]` | yes |

**Examples**

```bash
python3 .env-manager/manage.py next --client skillbox --format json --limit 5
```

**Validation**

```bash
python3 -m unittest tests.test_agent_ops_command_registry
```

**Graph Nodes**: `bead`, `check`, `command`, `evidence`, `service`

#### brain.search

Find commands, graph nodes, docs, Beads, checks, logs, and evidence by query.

- Surfaces: `cli`, `mcp`
- Scopes: None
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: `skillbox_search`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `format` | `enum[json\|text]?` | no |
| `limit` | `integer?` | no |
| `query` | `string` | yes |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `ok` | `boolean` | yes |
| `results` | `object[]` | yes |

**Examples**

```bash
python3 .env-manager/manage.py search "mcp parity" --format json
```

**Validation**

```bash
python3 -m unittest tests.test_agent_ops_command_registry
```

**Graph Nodes**: `command`, `doc`, `bead`, `check`, `log`, `evidence`

#### brain.snap

Record, diff, and replay runtime observation snapshots inside declared snapshot paths.

- Surfaces: `cli`, `mcp`
- Scopes: None
- Side effect: `local_write`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: `skillbox_snap`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `action` | `enum[create\|diff\|replay]` | yes |
| `fixture` | `string?` | no |
| `format` | `enum[json\|text]?` | no |
| `from` | `string?` | no |
| `name` | `string?` | no |
| `to` | `string?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `diff` | `object?` | no |
| `ok` | `boolean` | yes |
| `snapshot` | `object?` | no |

**Examples**

```bash
python3 .env-manager/manage.py snap --format json
```

```bash
python3 .env-manager/manage.py snap create --name before-agent-ops --format json
```

```bash
python3 .env-manager/manage.py snap create --write --name persisted --format json
```

```bash
python3 .env-manager/manage.py snap --format json replay tests/goldens/agent_ops_snapshot.json
```

```bash
python3 .env-manager/manage.py snap diff --from before.json --to after.json --format json
```

**Validation**

```bash
python3 -m unittest tests.test_agent_ops_command_registry
```

**Graph Nodes**: `snapshot`, `diff`, `evidence`

### runtime

#### runtime.capabilities

List agent-facing commands and tools with risk, side effects, and output contracts.

- Surfaces: `cli`, `mcp`
- Scopes: None
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: `sbp`
- MCP mirror: `skillbox_capabilities`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `compact` | `boolean?` | no |
| `format` | `enum[json]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `commands` | `object[]` | yes |
| `contract_version` | `string` | yes |
| `entrypoints` | `string[]` | yes |
| `ok` | `boolean` | yes |
| `registry` | `object` | yes |

**Examples**

```bash
python3 .env-manager/manage.py capabilities --format json
```

**Validation**

```bash
python3 -m unittest tests.test_agent_ops_command_registry
```

**Graph Nodes**: `command`, `mcp_tool`

## Tier 2

### runtime

#### runtime.cass_evidence

Measure skill invocations per repo from Cass, joined to policy; INCO when down. --proposals: read-only demote/promote candidates.

- Surfaces: `cli`
- Scopes: `cwd`
- Side effect: `network`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: `sbp`
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `format` | `enum[json\|text]?` | no |
| `proposals` | `boolean?` | no |
| `repo` | `string?` | no |
| `skill` | `string?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `applied` | `boolean` | yes |
| `cass_available` | `boolean` | yes |
| `command` | `string` | yes |
| `demotions` | `object[]` | yes |
| `promotions` | `object[]` | yes |
| `repos` | `object[]` | yes |
| `status` | `string` | yes |
| `structural_signals` | `string[]` | yes |

**Examples**

```bash
sbp evidence --repo /srv/skillbox/repos/example-app --format json
```

```bash
sbp evidence --proposals --format json
```

**Validation**

```bash
cd skillbox-config && python3 -m pytest tests/ -k evidence
```

**Graph Nodes**: `evidence`, `skill`, `repo`

#### runtime.doctor

Validate the internal repos/skills/logs/check graph for the selected scope.

- Surfaces: `cli`, `mcp`
- Scopes: `client`, `profile`
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: `skillbox_doctor`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `client` | `string?` | no |
| `format` | `enum[json\|text]?` | no |
| `profile` | `string[]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `checks` | `object[]` | yes |
| `next_actions` | `string[]` | yes |
| `ok` | `boolean` | yes |

**Examples**

```bash
python3 .env-manager/manage.py doctor --format json
```

**Validation**

```bash
python3 -m unittest tests.test_runtime_manager
```

**Graph Nodes**: `check`, `repo`, `skill`, `log`

#### runtime.down

Stop manageable services, stopping selected dependents before their prerequisites.

- Surfaces: `cli`, `mcp`
- Scopes: `client`, `profile`, `service`
- Side effect: `service`
- Risk: `medium`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: `skillbox_down`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `client` | `string?` | no |
| `dry_run` | `boolean?` | no |
| `profile` | `string[]?` | no |
| `service` | `string[]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `ok` | `boolean` | yes |
| `steps` | `object[]` | yes |

**Examples**

```bash
python3 .env-manager/manage.py down --profile surfaces --service api-stub
```

**Validation**

None

**Graph Nodes**: `service`

#### runtime.evidence

Read-only runtime evidence packet with stable keys and explicit blocked/gray conditions.

- Surfaces: `cli`
- Scopes: `cwd`, `profile`
- Side effect: `local_write`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `cwd` | `string?` | no |
| `format` | `enum[text\|json\|md]?` | no |
| `profile` | `string[]?` | no |
| `write` | `boolean?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `blocked_conditions` | `object[]` | yes |
| `kind` | `string` | yes |
| `next_actions` | `string[]` | yes |
| `overall` | `string` | yes |
| `scope` | `object` | yes |
| `sections` | `object` | yes |

**Examples**

```bash
python3 .env-manager/manage.py evidence --cwd "$PWD" --format json
```

**Validation**

```bash
python3 -m unittest tests.test_runtime_evidence
```

**Graph Nodes**: `evidence`, `check`, `service`, `skill`, `mcp_tool`

#### runtime.explain

Explain skill visibility provenance for one skill at one cwd: layer, scope rule, losers, and ranked fixes.

- Surfaces: `cli`, `mcp`
- Scopes: `client`, `profile`, `cwd`
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: `sbp`
- MCP mirror: `skillbox_explain_skill`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `cwd` | `string?` | no |
| `format` | `enum[json\|text]?` | no |
| `node` | `boolean?` | no |
| `skill` | `boolean?` | no |
| `target` | `string` | yes |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `layer` | `string?` | no |
| `lost` | `object[]` | yes |
| `remediation` | `object[]` | yes |
| `scope_rules` | `object[]` | yes |
| `skill` | `string` | yes |
| `visible` | `boolean` | yes |

**Examples**

```bash
python3 .env-manager/manage.py explain wiki --cwd "$PWD" --format json
```

**Validation**

```bash
python3 -m pytest tests/ -k explain
```

**Graph Nodes**: `skill`, `client`, `profile`

#### runtime.fleet_converge

Per-repo heal PLAN over the deduped fleet, grouped by triage class; each action carries its exact single-repo command. Plan only — never writes.

- Surfaces: `cli`
- Scopes: `client`, `profile`, `cwd`
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: `sbp`
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `all` | `boolean?` | no |
| `cwd` | `string?` | no |
| `format` | `enum[json\|text]?` | no |
| `max_depth` | `integer?` | no |
| `no_mcp` | `boolean?` | no |
| `scan_root` | `string[]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `classes` | `string[]` | yes |
| `dry_run` | `boolean` | yes |
| `kind` | `string` | yes |
| `next_actions` | `string[]` | yes |
| `repos` | `object[]` | yes |
| `summary` | `object` | yes |

**Examples**

```bash
python3 .env-manager/manage.py fleet converge --dry-run --format json
```

**Validation**

```bash
python3 -m pytest tests/ -k converge
```

**Graph Nodes**: `skill`, `repo`, `mcp_tool`, `command`

#### runtime.fleet_relink

Machine-migration rewrite: repoint other-machine links old-root->new-root when translated target is valid; else leave for converge.

- Surfaces: `cli`
- Scopes: `client`, `profile`, `cwd`
- Side effect: `local_write`
- Risk: `medium`
- Entrypoint: `manage.py`
- Owner binary: `sbp`
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `all` | `boolean?` | no |
| `apply` | `boolean?` | no |
| `cwd` | `string?` | no |
| `format` | `enum[json\|text]?` | no |
| `from_root` | `string?` | no |
| `max_depth` | `integer?` | no |
| `scan_root` | `string[]?` | no |
| `to_root` | `string?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `decisions` | `string[]` | yes |
| `dry_run` | `boolean` | yes |
| `kind` | `string` | yes |
| `next_actions` | `string[]` | yes |
| `repos` | `object[]` | yes |
| `roots` | `object` | yes |
| `summary` | `object` | yes |

**Examples**

```bash
python3 .env-manager/manage.py fleet relink --dry-run --format json
```

**Validation**

```bash
python3 -m pytest tests/test_fleet_relink.py
```

**Graph Nodes**: `skill`, `repo`, `command`

#### runtime.logs

Print recent log output for declared services.

- Surfaces: `cli`, `mcp`
- Scopes: `client`, `profile`, `service`
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: `skillbox_logs`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `client` | `string?` | no |
| `lines` | `integer?` | no |
| `profile` | `string[]?` | no |
| `service` | `string[]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `logs` | `object[]` | yes |
| `ok` | `boolean` | yes |

**Examples**

```bash
python3 .env-manager/manage.py logs --profile surfaces --service api-stub --lines 80
```

**Validation**

None

**Graph Nodes**: `log`, `service`

#### runtime.mcp_audit

Audit Claude/Codex MCP config parity; only undeclared servers count as drift.

- Surfaces: `cli`, `mcp`
- Scopes: `cwd`
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: `skillbox_mcp_audit`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `cwd` | `string?` | no |
| `format` | `enum[json\|text]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `ok` | `boolean` | yes |
| `servers` | `object[]` | yes |
| `unexplained_drift` | `object[]` | yes |

**Examples**

```bash
python3 .env-manager/manage.py mcp-audit --cwd "$PWD" --format json
```

**Validation**

```bash
python3 -m unittest tests.test_mcp_visibility
```

**Graph Nodes**: `mcp_tool`, `service`

#### runtime.mcp_sync

Render .mcp.json + .codex/config.toml from one MCP declaration; preserves operator entries.

- Surfaces: `cli`
- Scopes: `cwd`
- Side effect: `local_write`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `apply` | `boolean?` | no |
| `config_root` | `string?` | no |
| `cwd` | `string?` | no |
| `format` | `enum[json\|text]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `ok` | `boolean` | yes |
| `surfaces` | `object` | yes |
| `written` | `string[]` | yes |

**Examples**

```bash
python3 .env-manager/manage.py mcp sync --cwd "$PWD" --dry-run --format json
```

**Validation**

```bash
python3 -m unittest tests.test_mcp_render
```

**Graph Nodes**: `mcp_tool`, `service`

#### runtime.ports

List the machine-readable port registry for the active scope: owner, source, bind scope, and profiles.

- Surfaces: `cli`, `mcp`
- Scopes: `client`, `profile`, `service`
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: `skillbox_ports`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `client` | `string?` | no |
| `format` | `enum[json\|text]?` | no |
| `profile` | `string[]?` | no |
| `resolve` | `string?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `count` | `integer` | yes |
| `entries` | `object[]` | yes |
| `ok` | `boolean` | yes |
| `warnings` | `object[]` | yes |

**Examples**

```bash
python3 .env-manager/manage.py ports --format json
```

```bash
python3 .env-manager/manage.py ports --resolve api-stub --format json
```

**Validation**

```bash
python3 -m unittest tests.test_agent_ops_port_registry
```

**Graph Nodes**: `service`, `command`

#### runtime.pressure_report

Read-only disk pressure, protected buckets, worker target, and RCH/SBH posture.

- Surfaces: `cli`
- Scopes: None
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `format` | `enum[json\|text]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `next_actions` | `string[]` | yes |
| `ok` | `boolean` | yes |
| `pressure` | `object` | yes |

**Examples**

```bash
python3 .env-manager/manage.py pressure-report --format json
```

**Validation**

```bash
python3 -m unittest tests.test_pressure_report
```

**Graph Nodes**: `evidence`

#### runtime.registry_docs

Render the human-readable API reference from the shared command registry.

- Surfaces: `cli`
- Scopes: None
- Side effect: `local_write`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `format` | `enum[md\|json]?` | no |
| `write` | `boolean?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `abi_version` | `string` | yes |
| `bytes` | `integer` | yes |
| `content` | `string?` | no |
| `count` | `integer` | yes |
| `ok` | `boolean` | yes |
| `path` | `string` | yes |
| `sha256` | `string` | yes |
| `written` | `boolean` | yes |

**Examples**

```bash
python3 .env-manager/manage.py registry-docs --format md
```

```bash
python3 .env-manager/manage.py registry-docs --write --format json
```

**Validation**

```bash
python3 -m unittest tests.test_registry_docs
```

**Graph Nodes**: `command`, `doc`

#### runtime.render

Print the resolved internal runtime graph.

- Surfaces: `cli`, `mcp`
- Scopes: `client`, `profile`
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: `skillbox_render`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `client` | `string?` | no |
| `format` | `enum[json\|text]?` | no |
| `profile` | `string[]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `model` | `object` | yes |
| `ok` | `boolean` | yes |

**Examples**

```bash
python3 .env-manager/manage.py render --format json
```

**Validation**

None

#### runtime.skill_default

Set repo, cross-repo, or operator-global skill defaults with a dry-run/apply unified diff.

- Surfaces: `cli`
- Scopes: `cwd`
- Side effect: `local_write`
- Risk: `medium`
- Entrypoint: `manage.py`
- Owner binary: `sbp`
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `category` | `string?` | no |
| `cwd` | `string?` | no |
| `default_action` | `enum[on\|off]` | yes |
| `dry_run` | `boolean?` | no |
| `format` | `enum[json\|text]?` | no |
| `policy_path` | `string?` | no |
| `repos` | `string?` | no |
| `scope` | `enum[repo\|global\|repos\|category]` | yes |
| `skill_name` | `string` | yes |
| `yes` | `boolean?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `changed` | `boolean` | yes |
| `diff` | `string` | yes |
| `noop` | `boolean` | yes |
| `policy_path` | `string` | yes |
| `residue` | `object[]?` | no |
| `review` | `object?` | no |
| `targets` | `object[]?` | no |
| `validation` | `object[]?` | no |
| `would_change` | `boolean` | yes |

**Examples**

```bash
python3 .env-manager/manage.py skill default on wiki --repo --cwd "$PWD" --dry-run --format json
```

**Validation**

```bash
python3 -m unittest tests.test_skill_overrides tests.test_global_contract
```

**Graph Nodes**: `skill`, `repo`, `command`

#### runtime.skill_heal

Resolve a real skill source, durably pin it on for the current repo, link it, and return an activation packet.

- Surfaces: `cli`
- Scopes: `cwd`
- Side effect: `local_write`
- Risk: `medium`
- Entrypoint: `manage.py`
- Owner binary: `sbp`
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `cwd` | `string?` | no |
| `dry_run` | `boolean?` | no |
| `format` | `enum[json\|text]?` | no |
| `skill_name` | `string` | yes |
| `source` | `string?` | no |
| `verify` | `boolean?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `actions` | `object[]` | yes |
| `activation_packet` | `object?` | no |
| `changed` | `boolean` | yes |
| `noop` | `boolean` | yes |
| `override` | `object` | yes |
| `verification` | `object?` | no |

**Examples**

```bash
python3 .env-manager/manage.py skill heal wiki --cwd "$PWD" --format json
```

**Validation**

```bash
python3 -m unittest tests.test_skill_overrides
```

**Graph Nodes**: `skill`, `repo`, `command`

#### runtime.skill_lint

Lint repo-local skill override policy for contradictions, floor opt-outs, and stale skill refs.

- Surfaces: `cli`
- Scopes: `client`, `profile`, `cwd`
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: `sbp`
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `client` | `string?` | no |
| `cwd` | `string?` | no |
| `format` | `enum[json\|text]?` | no |
| `profile` | `string[]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `findings` | `object[]` | yes |
| `ok` | `boolean` | yes |
| `summary` | `object` | yes |

**Examples**

```bash
python3 .env-manager/manage.py skill lint --cwd "$PWD" --format json
```

**Validation**

```bash
python3 -m unittest tests.test_override_doctor
```

**Graph Nodes**: `skill`, `repo`, `check`, `command`

#### runtime.skill_off

Durably pin a skill off for the current repo and unlink current project installs.

- Surfaces: `cli`
- Scopes: `cwd`
- Side effect: `destructive`
- Risk: `destructive`
- Entrypoint: `manage.py`
- Owner binary: `sbp`
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `cwd` | `string?` | no |
| `dry_run` | `boolean?` | no |
| `format` | `enum[json\|text]?` | no |
| `skill_name` | `string` | yes |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `actions` | `object[]` | yes |
| `changed` | `boolean` | yes |
| `noop` | `boolean` | yes |
| `override` | `object` | yes |
| `summary` | `object` | yes |

**Examples**

```bash
python3 .env-manager/manage.py skill off wiki --cwd "$PWD" --dry-run --format json
```

**Validation**

```bash
python3 -m unittest tests.test_skill_overrides
```

**Graph Nodes**: `skill`, `repo`, `command`

#### runtime.skill_on

Durably pin a skill on for the current repo, link it, and return an activation packet.

- Surfaces: `cli`
- Scopes: `cwd`
- Side effect: `local_write`
- Risk: `medium`
- Entrypoint: `manage.py`
- Owner binary: `sbp`
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `cwd` | `string?` | no |
| `dry_run` | `boolean?` | no |
| `format` | `enum[json\|text]?` | no |
| `skill_name` | `string` | yes |
| `source` | `string?` | no |
| `verify` | `boolean?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `actions` | `object[]` | yes |
| `activation_packet` | `object?` | no |
| `changed` | `boolean` | yes |
| `noop` | `boolean` | yes |
| `override` | `object` | yes |
| `verification` | `object?` | no |

**Examples**

```bash
python3 .env-manager/manage.py skill on wiki --cwd "$PWD" --verify --format json
```

**Validation**

```bash
python3 -m unittest tests.test_skill_overrides
```

**Graph Nodes**: `skill`, `repo`, `command`

#### runtime.skill_what_if

Purely simulate effective skill visibility for repo, overlay, pin, opt-out, and machine inputs.

- Surfaces: `cli`
- Scopes: `cwd`
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: `sbp`
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `format` | `enum[json\|text]?` | no |
| `machine` | `string?` | no |
| `opt_out` | `string[]?` | no |
| `overlay` | `string[]?` | no |
| `pin` | `string[]?` | no |
| `repo` | `string` | yes |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `added` | `object[]` | yes |
| `effective` | `object[]` | yes |
| `pin_conflicts` | `object[]` | yes |
| `removed` | `object[]` | yes |
| `shadowed_by_layer` | `object[]` | yes |

**Examples**

```bash
python3 .env-manager/manage.py skill what-if --repo "$PWD" --overlay marketing --format json
```

**Validation**

```bash
python3 -m unittest tests.test_skill_overrides
```

**Graph Nodes**: `skill`, `repo`, `overlay`, `command`

#### runtime.skill_why

Explain one skill's cwd visibility provenance, including absence and exact durable fix commands.

- Surfaces: `cli`
- Scopes: `cwd`
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: `sbp`
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `cwd` | `string?` | no |
| `format` | `enum[json\|text]?` | no |
| `no_global` | `boolean?` | no |
| `no_project` | `boolean?` | no |
| `skill_name` | `string` | yes |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `layers` | `object[]` | yes |
| `next_actions` | `string[]` | yes |
| `remediation` | `object[]` | yes |
| `skill` | `string` | yes |
| `visible` | `boolean` | yes |
| `winning_layer` | `string?` | no |

**Examples**

```bash
python3 .env-manager/manage.py skill why wiki --cwd "$PWD" --format json
```

**Validation**

```bash
python3 -m unittest tests.test_skill_overrides
```

**Graph Nodes**: `skill`, `repo`, `command`

#### runtime.skills

Show effective skill visibility across global, client, and project-local layers.

- Surfaces: `cli`, `mcp`
- Scopes: `client`, `profile`, `cwd`
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: `sbp`
- MCP mirror: `skillbox_skills`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `client` | `string?` | no |
| `cwd` | `string?` | no |
| `issues_only` | `boolean?` | no |
| `profile` | `string[]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `issues` | `object[]` | yes |
| `ok` | `boolean` | yes |
| `skills` | `object[]` | yes |

**Examples**

```bash
python3 .env-manager/manage.py skills --client personal --cwd "$PWD"
```

**Validation**

```bash
python3 -m unittest tests.test_skill_visibility
```

**Graph Nodes**: `skill`, `client`, `profile`

#### runtime.state_backup

Create, list, and verify checksummed tar.gz backups of SKILLBOX_STATE_ROOT.

- Surfaces: `cli`
- Scopes: None
- Side effect: `local_write`
- Risk: `medium`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `action` | `enum[create\|list\|verify\|drill\|restore]?` | no |
| `backup_root` | `string?` | no |
| `format` | `enum[json\|text]?` | no |
| `i_understand_data_loss` | `boolean?` | no |
| `state_root` | `string?` | no |
| `target` | `string?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `backup` | `object?` | no |
| `backups` | `object[]?` | no |
| `checks` | `object[]?` | no |
| `evidence_path` | `string?` | no |
| `next_actions` | `string[]?` | no |
| `ok` | `boolean` | yes |
| `safety_backup` | `object?` | no |

**Examples**

```bash
python3 .env-manager/manage.py state-backup list --format json
```

```bash
python3 .env-manager/manage.py state-backup create --format json
```

```bash
python3 .env-manager/manage.py state-backup verify <manifest.json> --format json
```

```bash
python3 .env-manager/manage.py state-backup drill --format json
```

**Validation**

```bash
python3 -m unittest tests.test_state_backup
```

**Graph Nodes**: `command`

#### runtime.state_backup_drill

Extract the newest state backup into a temp dir and write restore-drill evidence.

- Surfaces: `cli`
- Scopes: None
- Side effect: `local_write`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `backup_root` | `string?` | no |
| `format` | `enum[json\|text]?` | no |
| `state_root` | `string?` | no |
| `target` | `string?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `checks` | `object[]` | yes |
| `evidence_path` | `string` | yes |
| `manifest` | `string` | yes |
| `ok` | `boolean` | yes |

**Examples**

```bash
python3 .env-manager/manage.py state-backup drill --format json
```

**Validation**

```bash
python3 -m unittest tests.test_state_backup
```

**Graph Nodes**: `command`

#### runtime.state_backup_restore

Restore a verified state backup after pulse, checksum, confirmation, and safety-backup guardrails.

- Surfaces: `cli`
- Scopes: None
- Side effect: `destructive`
- Risk: `destructive`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `backup_root` | `string?` | no |
| `format` | `enum[json\|text]?` | no |
| `i_understand_data_loss` | `boolean` | yes |
| `state_root` | `string?` | no |
| `target` | `string?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `checks` | `object[]` | yes |
| `ok` | `boolean` | yes |
| `safety_backup` | `object` | yes |
| `state_root` | `string` | yes |

**Examples**

```bash
python3 .env-manager/manage.py state-backup restore <manifest.json> --i-understand-data-loss --format json
```

**Validation**

```bash
python3 -m unittest tests.test_state_backup
```

**Graph Nodes**: `command`

#### runtime.status

Summarize repo, artifact, skill, task, service, log, and health state for the active scope.

- Surfaces: `cli`, `mcp`
- Scopes: `client`, `profile`, `service`
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: `skillbox_status`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `client` | `string?` | no |
| `format` | `enum[json\|text]?` | no |
| `profile` | `string[]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `ok` | `boolean` | yes |
| `repos` | `object[]` | yes |
| `services` | `object[]` | yes |
| `tasks` | `object[]` | yes |

**Examples**

```bash
python3 .env-manager/manage.py status --format json
```

**Validation**

```bash
python3 -m unittest tests.test_cli_units
```

**Graph Nodes**: `service`, `task`, `repo`, `check`

#### runtime.structure_doctor

One front door for all STRUCTURAL gates with INCO/FAIL/PASS; invokes the runtime doctor as a runtime gate.

- Surfaces: `cli`
- Scopes: `cwd`
- Side effect: `none`
- Risk: `low`
- Entrypoint: `manage.py`
- Owner binary: `sbp`
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `cwd` | `string?` | no |
| `format` | `enum[json\|text]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `exit_code` | `integer` | yes |
| `gates` | `object[]` | yes |
| `ok` | `boolean` | yes |
| `summary` | `object` | yes |

**Examples**

```bash
python3 .env-manager/manage.py structure-doctor --cwd "$PWD" --format json
```

**Validation**

```bash
python3 -m pytest tests/ -k doctor
```

**Graph Nodes**: `check`, `skill`, `mcp_tool`

#### runtime.sync

Create managed repo/artifact/log directories and install declared skills for the scope.

- Surfaces: `cli`, `mcp`
- Scopes: `client`, `profile`
- Side effect: `local_write`
- Risk: `medium`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: `skillbox_sync`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `client` | `string?` | no |
| `dry_run` | `boolean?` | no |
| `profile` | `string[]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `ok` | `boolean` | yes |
| `steps` | `object[]` | yes |

**Examples**

```bash
python3 .env-manager/manage.py sync --client personal --dry-run
```

**Validation**

None

**Graph Nodes**: `repo`, `artifact`, `skill`, `log`

#### runtime.up

Sync runtime state, run service bootstrap tasks, and start manageable services.

- Surfaces: `cli`, `mcp`
- Scopes: `client`, `profile`, `service`
- Side effect: `service`
- Risk: `medium`
- Entrypoint: `manage.py`
- Owner binary: None
- MCP mirror: `skillbox_up`

**Inputs**

| Name | Type | Required |
|---|---|---|
| `client` | `string?` | no |
| `dry_run` | `boolean?` | no |
| `profile` | `string[]?` | no |
| `service` | `string[]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `ok` | `boolean` | yes |
| `services` | `object[]` | yes |
| `steps` | `object[]` | yes |

**Examples**

```bash
python3 .env-manager/manage.py up --profile surfaces --service api-stub
```

**Validation**

None

**Graph Nodes**: `service`, `task`

### outer

#### outer.doctor

Run outer repo drift and readiness checks: manifests, Compose wiring, skill-repo sync.

- Surfaces: `cli`
- Scopes: None
- Side effect: `none`
- Risk: `low`
- Entrypoint: `04-reconcile.py`
- Owner binary: None
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `format` | `enum[json\|text]?` | no |
| `skip_compose` | `boolean?` | no |
| `skip_skill_sync` | `boolean?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `checks` | `object[]` | yes |
| `ok` | `boolean` | yes |

**Examples**

```bash
python3 scripts/04-reconcile.py doctor --format json --skip-compose --skip-skill-sync
```

**Validation**

```bash
python3 -m unittest tests.test_reconcile
```

**Graph Nodes**: `check`

#### outer.render

Print the resolved outer sandbox model.

- Surfaces: `cli`
- Scopes: None
- Side effect: `none`
- Risk: `low`
- Entrypoint: `04-reconcile.py`
- Owner binary: None
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `format` | `enum[json\|text]?` | no |
| `with_compose` | `boolean?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `model` | `object` | yes |
| `ok` | `boolean` | yes |

**Examples**

```bash
python3 scripts/04-reconcile.py render --with-compose
```

**Validation**

```bash
python3 -m unittest tests.test_reconcile
```

### box

#### box.down

Tear down a remote box, destroying the droplet and removing Tailscale enrollment.

- Surfaces: `cli`, `make`
- Scopes: `box`
- Side effect: `destructive`
- Risk: `destructive`
- Entrypoint: `box.py`
- Owner binary: None
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `box_id` | `string` | yes |
| `dry_run` | `boolean?` | no |
| `format` | `enum[json\|text]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `ok` | `boolean` | yes |
| `steps` | `object[]` | yes |

**Examples**

```bash
python3 scripts/box.py down <box-id> --dry-run --format json
```

**Validation**

```bash
python3 -m unittest tests.test_box_lifecycle
```

**Graph Nodes**: `box`

#### box.status

Health-check a remote box: phone URL, public SSH, Tailnet ping, MagicDNS, posture violations.

- Surfaces: `cli`, `make`
- Scopes: `box`
- Side effect: `network`
- Risk: `low`
- Entrypoint: `box.py`
- Owner binary: None
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `box_id` | `string` | yes |
| `format` | `enum[json\|text]?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `checks` | `object[]` | yes |
| `ok` | `boolean` | yes |
| `violations` | `object[]` | yes |

**Examples**

```bash
make box-status BOX=<id>
```

**Validation**

```bash
python3 -m unittest tests.test_box_lifecycle
```

**Graph Nodes**: `box`, `check`

#### box.up

Provision a new remote box (DigitalOcean + Tailscale) or resume a partial provision.

- Surfaces: `cli`, `make`
- Scopes: `box`, `profile`
- Side effect: `network`
- Risk: `high`
- Entrypoint: `box.py`
- Owner binary: None
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `box_id` | `string` | yes |
| `dry_run` | `boolean?` | no |
| `profile` | `string?` | no |
| `resume` | `boolean?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `box` | `object` | yes |
| `ok` | `boolean` | yes |
| `steps` | `object[]` | yes |

**Examples**

```bash
python3 scripts/box.py up <box-id> --profile dev-small --dry-run --format json
```

**Validation**

```bash
python3 -m unittest tests.test_box_lifecycle
```

**Graph Nodes**: `box`

### make

#### make.dev_sanity

Validate the internal runtime graph, filesystem readiness, and managed skill integrity.

- Surfaces: `make`
- Scopes: None
- Side effect: `none`
- Risk: `low`
- Entrypoint: `Makefile`
- Owner binary: None
- MCP mirror: None

**Inputs**

None

**Outputs**

| Name | Type | Required |
|---|---|---|
| `checks` | `object[]` | yes |
| `ok` | `boolean` | yes |

**Examples**

```bash
make dev-sanity
```

**Validation**

None

**Graph Nodes**: `check`

### clipboard

#### clipboard.paste

Report, diagnose, or explain exact-route seamless-paste readiness with redacted evidence.

- Surfaces: `cli`
- Scopes: `profile`
- Side effect: `network`
- Risk: `low`
- Entrypoint: `clipboard-paste`
- Owner binary: None
- MCP mirror: None

**Inputs**

| Name | Type | Required |
|---|---|---|
| `command` | `enum[status\|doctor\|explain]?` | no |
| `json` | `boolean?` | no |
| `probe_target` | `boolean?` | no |
| `profile` | `string?` | no |
| `route_path` | `string?` | no |

**Outputs**

| Name | Type | Required |
|---|---|---|
| `agent` | `object` | yes |
| `capabilities` | `object` | yes |
| `checks` | `object[]` | yes |
| `fallback` | `object` | yes |
| `generated_at` | `number` | yes |
| `install` | `object` | yes |
| `last_receipt` | `string?` | no |
| `profile` | `string` | yes |
| `redaction` | `string` | yes |
| `route` | `object` | yes |
| `schema_version` | `integer` | yes |
| `state` | `enum[ready\|configured\|degraded\|stale\|unsupported\|ambiguous\|offline]` | yes |
| `target` | `string?` | no |
| `target_probe` | `object` | yes |
| `version` | `string?` | no |

**Examples**

```bash
clipboard-paste status --profile d3 --json
```

```bash
clipboard-paste doctor --profile d3 --probe-target --json
```

```bash
clipboard-paste explain --profile d3 --json
```

**Validation**

```bash
python3 -m unittest tests.test_clipboard_status tests.test_agent_ops_command_registry
```

**Graph Nodes**: `command`, `check`, `evidence`
