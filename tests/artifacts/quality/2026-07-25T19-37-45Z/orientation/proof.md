# Agent ops brain safe-orientation proof

- corpus_schema_version: `2026-07-25+agent_ops_brain.orientation`
- python: `3.12.3`
- status: `PASS`

```
BASELINE SCORECARD -- agent ops brain safe orientation

category            cases   pass  false_safe  false_abstain
-----------------------------------------------------------
healthy                 3      3           0              0
degraded                3      3           0              0
conflicting             3      3           0              0
missing_evidence        3      3           0              0
unsafe_action           3      3           0              0
abstention              5      5           0              0
-----------------------------------------------------------
TOTAL                  20     20           0              0

false_safe findings   : 0
false_abstain findings: 0
abstention required   : 13 scenario(s); brain abstained in 14

baseline: matched (no drift)
```
