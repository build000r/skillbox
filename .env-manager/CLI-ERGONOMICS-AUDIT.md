# CLI Ergonomics Audit — manage.py / sbp / snap

Bead: skillbox-gkso (broad pass) + skillbox-ej24 (focused snap fix).
Skill: agent-ergonomics-and-intuitiveness-maximization-for-cli-tools.
Date: 2026-07-12. Mode: full (audit + apply + tests). Profile: Claude Code.

Surfaces audited (agent-facing, high-use):
- `python3 .env-manager/manage.py` (runtime graph / agent-ops brain CLI; impl `runtime_manager/cli.py`)
- `scripts/sbp` (personal skillbox runtime + skill helper; bash wrapper over manage.py)
- `snap` (subcommand of manage.py; impl `runtime_manager/cli.py` `_handle_snap`, `_build_parser` snap block)

Scoring: 0–1000 per dimension. Scores are pre-pass -> post-pass where a fix landed.
Evidence: runtime invocation transcripts captured during this pass (see commands inline).

## Scorecard (weighted for Claude Code profile)

| Surface | Dimension | Pre | Post | Evidence / note |
|---------|-----------|-----|------|-----------------|
| snap | agent_intuitiveness | 450 | 850 | Pre: bare `snap --format json` returned a discovery menu, not data; agent expecting sibling-style data (like `status --format json`) had to retry with `create`. Post: bare `snap --format json` creates a read-only snapshot directly. |
| snap | agent_ergonomics | 500 | 850 | Canonical snapshot now one command (`snap --format json`) instead of two (`snap` -> read menu -> `snap create`). |
| snap | self_documentation | 700 | 800 | `snap --help` lists `{create,diff,replay,actions}` + direct create flags; discovery menu preserved at `snap actions`; help text documents the bare form. |
| snap | composability | 750 | 800 | Pure JSON stdout, clean stderr, exit 0 on success. `snap actions` keeps the machine-readable subcommand map. |
| snap | regression_resistance | 400 | 800 | New tests pin bare-json snapshot, `snap actions` menu, and MCP/CLI parity (test_cli_units.py). |
| manage.py | output_parseability | 500 | 850 | Pre: `manage.py <wrong> --format json` emitted argparse text usage on stderr (unparseable). Post: emits a JSON error envelope (`USAGE_ERROR`) on stdout, exit 2, clean stderr. |
| manage.py | error_pedagogy | 650 | 850 | JSON envelope names the typo suggestion (`suggestions[].command`) + exact `next_actions` (`manage.py status --format json`, `manage.py capabilities --format json`). Text mode unchanged (already had "Did you mean"). |
| manage.py | intent_inference | 800 | 850 | difflib close-match command suggestions already existed; now surfaced in structured form for JSON callers too. |
| manage.py | agent_intuitiveness | 800 | 800 | Already strong: capabilities/robot-docs/robot-triage present, `--json`/`--jason`/`--jsno` aliases, default command. |
| manage.py | composability | 850 | 850 | stdout data / stderr diagnostics split verified; exit-code dictionary in capabilities. |
| sbp | error_pedagogy | 600 | 800 | Pre: genuinely-unknown commands (e.g. `bogussub`) got a canned "Did you mean: sbp status" — misleading. Post: suggestion emitted only on a plausible match; unknowns point to capabilities. More typo mappings added. |
| sbp | intent_inference | 600 | 780 | Added typo mappings: recalibrat/recal, doc/docter, cand/candidate, mc/mcpp, skils, statuss, logss. |
| sbp | output_parseability | 800 | 800 | capabilities/robot-triage/status --json parseable; stdout/stderr contract documented in capabilities. |
| sbp | agent_ease_of_use | 800 | 800 | Rich `--help`, `capabilities --json`, `robot-docs guide`, `robot-triage`. |

Median dimension uplift on touched surfaces: ~+150. No surface regressed.

## Applied changes (this pass)

1. **snap: `--format` accepted directly like siblings (skillbox-ej24).**
   `runtime_manager/cli.py`: bare `snap` (no verb) defaults to the `create` action;
   canonical create flags (`--name/--write/--cwd/--ntm-session/--no-adapters`) added
   to the top-level `snap` parser; new `snap actions` verb surfaces the discovery menu
   (former bare-snap payload); usage payload gains `default_action: create` and leads
   with `snap --format json`. `create`/`diff`/`replay` remain fully supported.
   MCP `skillbox_snap` with omitted action now creates a read-only snapshot (parity
   with the CLI). Follow-up (not owned here): update the mcp_server.py `skillbox_snap`
   description string ("Omit to return read-only usage") to say "Omit to create a
   read-only snapshot; use action=actions for the menu".

2. **manage.py: JSON-mode argparse errors -> parseable envelope (gkso).**
   `runtime_manager/cli.py`: `SkillboxArgumentParser.error` detects `--format json`
   (or a json alias) in argv and emits a `brain_error_payload` `USAGE_ERROR` envelope
   on stdout (exit 2, clean stderr) with typo `suggestions` + copy-paste `next_actions`.
   Text mode is unchanged.

3. **sbp: honest unknown-command suggestions (gkso).**
   `scripts/sbp`: the unknown-command fallback no longer emits a canned
   `Did you mean: sbp status` for commands with no plausible match; the "Did you mean"
   line prints only when a typo mapping matches. Added common typo mappings.

## Tests added / updated

- `tests/test_cli_units.py`:
  - `test_snap_bare_json_creates_snapshot_like_siblings` (ej24 — first-try success)
  - `test_snap_bare_text_creates_snapshot`
  - `test_snap_actions_returns_structured_usage_payload`
  - `test_skillbox_snap_without_action_creates_snapshot_matching_cli` (MCP/CLI parity)
  - `test_skillbox_snap_actions_returns_usage_payload`
  - `test_unknown_command_error_text_mode_suggests_correct_command`
  - `test_unknown_command_error_json_mode_returns_parseable_envelope` (gkso)
- `tests/test_cli_wrappers.py`:
  - `test_sbp_genuinely_unknown_command_omits_misleading_suggestion` (gkso)
  - `test_sbp_close_typo_still_suggests_exact_command`

## Verification

- `python3 -m pytest tests/test_cli_wrappers.py tests/test_cli_units.py tests/test_agent_ops_snapshots.py tests/test_agent_ops_golden_outputs.py tests/test_registry_docs.py -q -k "not test_agent_ops_brain_mcp_dispatch_matches_cli_representatives"` -> 102 passed.
- Smoke: `manage.py --help`, `sbp --help`, `manage.py snap --help` all exit 0.
- stdout/stderr split verified for `status --format json` and JSON-mode usage errors (stderr 0 bytes).

## Known pre-existing issue (NOT caused by this pass; human follow-up)

- `tests/test_cli_units.py::test_agent_ops_brain_mcp_dispatch_matches_cli_representatives`
  fails on a clean tree (verified via `git stash`). The `search` MCP dispatch payload
  is missing a `hits` key that the CLI payload has (MCP/CLI `search` parity drift).
  Unrelated to manage.py/sbp/snap ergonomics. Left for a separate fix.

## Accepted-by-design (not fixed)

- `snap create` snapshot_id varies run-to-run even with `--created-at` pinned, because
  it content-addresses over *live* runtime/evidence state. Deterministic reproduction is
  the `snap replay <fixture>` path (golden fixture is stable). This is correct behavior.
- manage.py text-mode usage errors remain plain text on stderr (human-facing); only the
  JSON path was structured, matching the "stdout data / stderr diagnostics" contract.
