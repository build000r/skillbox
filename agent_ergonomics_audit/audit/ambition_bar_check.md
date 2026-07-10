# Ambition Bar Check

Question: Did this pass materially improve the cold-agent path through Skillbox's CLI surfaces, or merely document existing behavior?

Answer: It improved the path. The pass did not stop at a scorecard. It shipped narrowly scoped fixes on the documented brain surfaces that a cold agent is expected to run first:

- `capabilities` now advertises an executable `search` safe-first command.
- Brain `next_actions` now use real `python3 .env-manager/manage.py ...` commands instead of internal `brain.*` labels.
- `explain next` resolves to `command:brain.next`.
- `graph --algorithm pagerank --format json` returns structured JSON instead of argparse text.
- `snap --format json` returns an agent-readable action list instead of parser usage.

The pass also refused to hide larger safety issues as "done": SBP JSON/mutation hardening and direct box CLI safety were filed as follow-up beads with acceptance criteria.

Ambition bar result: met for a bounded full pass on bead `.7`; remaining systemic work is tracked in `.1`, `.2`, `.5`, `.6`, `.8`, and `.9`.
