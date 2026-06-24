# Skill Overrides Swarm Contract

This contract governs long-running swarm work on the durable skill-overrides
epic. Its job is to make observer check-ins useful without interrupting an
agent that is editing, testing, or waiting on a long command.

## Live Lane Identity

Before scheduling or sending any check-in, record both identifiers below in the
lane's marching orders:

- tmux target: the `session:window.pane` value from `sbp send-later panes --rich`
  or `sbp send-later panes --json --rich`
- Beads actor: the agent/person who claimed the active `br` issue

Immediately before any check-in is sent, verify the target pane again with
`sbp send-later panes --json --rich` or `ntm --robot-snapshot --robot-format=json`.
The check-in is allowed only if the live pane still matches the recorded target
and actor. Do not infer identity from a foreground file read.

## Expected Artifacts

Each phase must name its expected artifacts before launch. For the current
override epic, use this checklist unless a child bead narrows it further:

| Phase | Expected Artifacts | Verification |
| --- | --- | --- |
| Schema and git tracking | `.skillbox/skill-overrides.yaml`, `tests/test_skill_overrides.py`, `tests/test_overrides_gitignore.py` | focused unittest targets plus full `python3 -m unittest discover -s tests` when practical |
| Precedence and explanations | runtime visibility code, `tests/test_skill_overrides.py`, explain/visibility tests | adjacent-precedence tests and explain parity |
| Write safety and firewall | override writer, lifecycle prune planner, `tests/test_skill_overrides.py` | crash/concurrency tests and prune dry-run/apply parity |
| Agent command surfaces | `scripts/sbp`, runtime CLI handlers, `docs/SBP_OUTPUT_SCHEMAS.md`, SBP skill docs | generated schema freshness plus focused CLI tests |

## Close Marker

A lane is complete only when the owning issue is closed in Beads and the close
reason names the verification command output. Preferred close marker:

```bash
br close <issue-id> --reason "Implemented <summary>. Verified: <commands and final status>."
```

If the lane is not complete, leave a Beads comment or handoff note that names
the last safe state, the next command, and any files currently being edited.

## Polling Window

The first observer poll must wait 20-30 minutes after lane launch. Earlier
polls create noise while caches warm, tests run, or the agent is still reading.
After the first poll, use a cooldown of at least 20 minutes unless the pane is
explicitly idle and the issue owner requested tighter supervision.

## No-Nudge Conditions

Do not nudge when any of these are true:

- the pane tail is changing between checks
- a test command, package command, git command, or editor-driven patch is active
- the pane shows a busy indicator rather than an idle footer
- the live pane identity no longer matches the recorded target and actor
- a foreground file read disagrees with the pane state

The pane's own robot/tmux state wins over stale foreground observations.

## Send-Later Timer

Use `sbp send-later --when-waiting` for automated check-ins. Preview first,
then schedule the same target after identity verification:

```bash
sbp send-later schedule --to <target> --in 25m \
  --message "observer check-in: report issue id, current command, and next safe step" \
  --when-waiting --cooldown-minutes 20 --max-fires 1 --dry-run
```

If the dry run resolves to the expected tmux target, remove `--dry-run` and
schedule it. For recurring supervision, also add `--recurring` and a bounded
`--max-fires` value.

## Marching Orders Reference

Every swarm lane implementing a skill-overrides child issue must include this
line in its marching orders:

```text
Observer contract: follow docs/skill-overrides-swarm-contract.md; do not send
check-ins until the live tmux target and Beads actor are verified.
```
