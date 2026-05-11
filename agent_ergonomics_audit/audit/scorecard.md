# Agent Ergonomics Scorecard

Primary target: `python3 .env-manager/manage.py`

| Surface | Pre-pass finding | Post-pass state |
| --- | --- | --- |
| Machine-readable contract | Missing | `capabilities --json` returns tool, commands, entrypoints, exit codes, env vars, and next actions. |
| In-tool agent guide | Missing | `robot-docs guide` gives start commands, structured-output rules, and safe mutation guidance. |
| Mega-command | Missing | `--robot-triage` returns quick ref, recommendations, commands, and graph health. |
| JSON typo inference | Missing | `--json`, `--jsno`, `--jason`, and `--jsson` normalize to `--format json`; typo notices go to stderr. |
| Unknown command pedagogy | Generic argparse error | Adds capabilities hint and nearest-command suggestion. |

Median scored surface uplift estimate: +250 points across the applied surfaces.
