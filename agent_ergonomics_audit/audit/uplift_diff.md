# Uplift Diff

- `manage:capabilities`: new surface; self_documentation and output_parseability moved from absent/0 to high-confidence JSON contract.
- `manage:robot-docs`: new surface; external documentation lookup no longer required for agent start path.
- `manage:robot-triage`: new surface; three common inspection calls collapse into one JSON packet.
- `manage:json-aliases`: common agent typo path moved from argparse failure to inferred-and-acted.
- `manage:unknown-command`: misspelled command path now teaches the nearest valid command and points to the capabilities contract.

No regressions observed in focused tests.
