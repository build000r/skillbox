# Agent Ergonomics Handoff — after Pass 2

Date: 2026-08-14 · Pass 2 ran on main, host checkout (pass 1 was in-container).

## What landed (11 commits, a290d80..4996a30 + help console 95be3de earlier)

See `audit/applied_changes.jsonl` and `audit/scorecard_pass_2.md`. Headline:
box.py real down/upgrade are triple-gated (confirmation, clean tree, dry-run
marker interoperable with the operator MCP store); sbp help is single-sourced
from atlas() with set-equality drift tests; capabilities is complete, executed
in CI (safe_first_try smoke gate), and declares sbp the canonical front door;
the doctor family routes through sbp doctor.

## Queued for Pass 3 / adjacent passes

1. skillbox-387k: `box.py exec` verb (read-only classification + marker) — with
   skillbox-mxw7 (gated compose-down JSON) these unblock
   skillbox-mcp-deprecation-epic-vniq.4 (retire operator_mcp_server). A thin
   operator skill must carry the gating story before retirement.
2. skillbox-hws5: doctor envelope/vocabulary unification — natural first target
   for the world-class-doctor-mode pass (which also wants --fix/undo/run
   artifacts: see doctor_gap rows in audit/partial/pass2/doctor_family.jsonl).
3. skillbox-0d87: EXIT_DRIFT=2 vs argparse-2 collision (family-wide).
4. skillbox-sbp-repo-atlas-repair-2gbo: repo atlas engine; smoke gate forces
   un-skip on heal (tests/test_sbp_capabilities_smoke.py KNOWN_BROKEN).
5. Not yet addressed from scorer output: status/session lowercase error codes,
   search `examples` vs next_actions divergence, box.py dual error envelopes
   (structured_error vs structured_cli_error), `ssh` capabilities entry still
   pointing agents at MCP operator_box_exec (update when 387k lands).

## Verification to run before closing pass 3

python3 -m unittest tests.test_sbp_wrapper_contract tests.test_sbp_help_human \
  tests.test_sbp_capabilities_smoke tests.test_box_mutation_gates \
  tests.test_structure_doctor tests.test_cli_units tests.test_agent_ops_graph \
  tests.test_output_schema_docs
Containerized `make self-test` for the canonical gate (raw-host full discover is
a known-bad signal on macOS).
