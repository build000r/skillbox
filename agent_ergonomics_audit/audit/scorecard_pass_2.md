# Agent Ergonomics Scorecard — Pass 2

Date: 2026-08-14 · Target: skillbox @ 3b41382 (pass start) → HEAD (pass end)
Mode: full · Branch: main (no new branch) · Applier: single (flock absent on macOS)

## Inventory

Four parallel scorer families at HEAD 3b41382 (evidence-gated: >700 requires
file:line or invocation transcript). Raw rows: `audit/partial/pass2/*.jsonl`.

| Family | Surfaces | Findings | Notable |
|---|---|---|---|
| sbp wrapper | 19 | 9 (0 P0, 3 P1) | best: sbp git; worst: repo, send-later, json-alias silent swallow |
| box.py + operator parity | 16 | 10 (1 P0, 1 P1) + 7 parity gaps | P0: ungated one-call droplet destroy |
| manage.py brain + envelope | 13 | 10 (0 P0, 1 P1) | envelope unification (086q.6) substantially verified |
| doctor family | 8 | 10 (0 P0, 4 P1) + 8 doctor gaps | 4 doctors, 3 vocabularies, contradictory verdicts |

## Closed-bead verification at HEAD

- 086q.6 unified error envelope — SUBSTANTIALLY HOLDS (divergences: graph ok, status
  lowercase codes, search examples field; graph fixed this pass)
- 086q.8 sbp JSON/mutation contract — MOSTLY HOLDS (safe_first_try regressions repo/send-later;
  send-later fixed this pass, repo bead-tracked + smoke-pinned)
- 086q.9 box.py safety — HOLDS but was fail-open by default and untested (fixed this pass)

## Applied (11 substantive commits)

R-206 graph ok parity · R-204 unknown-flag rejection · R-205 send-later portability ·
R-203 safe_first_try smoke gate · R-201 box.py mutation gates (P0) · R-211 posture-proof
+ status --no-probe · R-208 doctor-family routing · R-210 capabilities completeness +
front-door role · R-207 runtime-verb envelopes · help single-source refactor · R-212 repo
error pedagogy. Details: `audit/applied_changes.jsonl`.

## Deferred (beads)

- skillbox-387k — box.py exec verb (blocks vniq.4)
- skillbox-mxw7 — gated compose-down JSON (blocks vniq.4)
- skillbox-hws5 — doctor envelope/vocabulary unification (doctor-mode pass candidate)
- skillbox-0d87 — EXIT_DRIFT=2 vs argparse-2 collision (family-wide exit-code change)
- skillbox-sbp-repo-atlas-repair-2gbo — repo atlas engine repair (pre-existing)

## Dimension movement (qualitative; re-score at next pass start)

- safety_with_recovery: box.py down/upgrade from ungated (F-box-01 P0) to triple-gated → largest uplift
- intent_inference/error_pedagogy: silent flag swallow eliminated; robot-docs topics validated; repo/send-later errors teach
- regression_resistance: +4 new test files/gates (smoke, mutation gates, drift, help-atlas equality)
- self_documentation: capabilities complete + role block; doctor routing discoverable; help single-sourced
