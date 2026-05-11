# Ambition Bar Check

- Substantive surface changes: 5
- Dimensions touched: agent_intuitiveness, agent_ergonomics, output_parseability, error_pedagogy, intent_inference, self_documentation, composability, regression_resistance
- Mega-command: yes, `--robot-triage`
- Capabilities or robot-docs: yes, both
- JSON or robot read-side output: yes, `capabilities --json`, `--robot-triage`, and JSON aliases
- Error rewrite: yes, unknown command suggestions
- Intent inference: yes, JSON typo aliases

Self-prompt:

> That's it?? I was hoping you would get a lot more practical value out of this skill.
> Where are the dramatic improvements? Re-read the playbook, look at the surfaces still
> scoring below 500 on output_parseability / error_pedagogy / intent_inference /
> self_documentation, and ship a substantially larger batch of high-leverage changes.
> You're allowed to be ambitious. Default to acting, not deliberating.

Result: the second-round scope is deferred to avoid trampling a dirty worktree already carrying unrelated runtime-manager changes. The next highest-leverage batch is extending the same contract to `scripts/04-reconcile.py`, `scripts/box.py`, and wrapper shortcuts.
