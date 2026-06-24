# Skill Overrides Swarm Marching Orders

Use this workgraph for any long-running swarm or headless lane implementing the
durable skill-overrides epic.

Observer contract: follow
[`docs/skill-overrides-swarm-contract.md`](../../docs/skill-overrides-swarm-contract.md);
do not send check-ins until the live tmux target and Beads actor are verified.

Before launching a lane, record:

- the claimed Beads issue id
- the Beads actor
- the tmux target from `sbp send-later panes --json --rich`
- expected artifacts for that issue
- the first safe observer poll window, 20-30 minutes after launch

Use `sbp send-later schedule --when-waiting` for automated observer check-ins.
Never nudge a pane that is mid-edit, running tests, streaming output, or whose
identity cannot be verified from live pane state.
