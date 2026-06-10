---
name: smart
description: Identify the single most accretive, highest-leverage next move, then by default define a goal sequence that encompasses it, turn it into a set of no-ragrets beads, and pursue the goal. Use when tagged with /smart, when the user wants to identify the highest-leverage next move, most impactful addition, or smartest thing to do next, when the user asks for smart goal mode, ambitious goal tracker, Gantt or ganntt plan, or when the user wants /smart to continue, iterate, or loop through bounded rounds until a concrete goal or success criterion is met.
depends_on:
  - no-ragrets          # Step 2 wraps each goal in the regret-minimization success contract
  - beads-workflow      # Step 2 converts the goal sequence into a br Beads graph
  - divide-and-conquer  # Step 3 pursues substantial slices through the ready frontier
---

# Smart

## First Progress Marker (Required)

Start the first progress update with the exact prefix `Using smart`.

Preferred format: `Using smart to <goal>. First I will <next concrete step>.`

Do not change or omit that prefix. Reliability review tooling treats it as a stable invocation marker.

You've been invoked to deliver the single highest-leverage insight for whatever the user is working on right now.

## Read-before-Edit Invariant

Before calling Edit on any file, you MUST first call Read on that file in this
conversation. The Edit tool will reject calls on files that have not been Read.
Never assume file contents from memory or prior sessions — always Read first,
then Edit. This applies to every file modification: skill templates, code,
config, docs, and generated views. Skipping Read wastes tool calls on retries
and breaks the edit cycle.

## The Question

Ground yourself in everything available — the conversation so far, the codebase, recent git history, any active plans or tasks — then answer this:

> **What is the single smartest, most radically innovative, accretive, useful, and compelling thing you could do or suggest at this point to get us on the right track?**

## Default Operating Procedure

Every `smart` call runs this three-step procedure by default:

1. **Define a goal sequence that encompasses all of that.** Synthesize the
   repo-integrity pass, the single highest-leverage move, and the adjacent
   concerns into an *ordered sequence of goals* that gets the project "on the
   right track."

2. **Turn it into a set of `$no-ragrets` beads.** Run each goal in the sequence
   through the `no-ragrets` success contract (`Outcome` / `Evidence` /
   `Failure avoided`) and mint it as a `br` bead with dependency edges.

3. **Pursue the goal.** Begin executing the ready frontier instead of handing the
   sequence back as advice. Route bounded single changes to direct local work;
   route large-ish, multi-file, UI-facing, parallel, or review-sensitive slices
   through `/divide-and-conquer`.

## How to Answer

1. **Absorb context first.** Read the conversation history, check `git log --oneline -20` and `git diff --stat` for recent momentum.
2. **Scope to what matters now.** Narrow to the current bottleneck or highest-value gap.
3. **Be concrete and actionable.** Name files, functions, architectural decisions.
4. **Be bold.** Swing for impact — the move they haven't thought of.
5. **One committed direction.** Not a menu of five competing options.

## Choosing the Execution Posture

- Use direct local work when the next iteration is a single bounded change.
- Use `/divide-and-conquer` when an iteration is large-ish, multi-file, UI-facing, naturally parallel, or review-sensitive.

## Smart Loop Frame

Every `/smart` invocation is a loop frame. Always define:

1. `loop_goal` — the concrete outcome this recommendation is steering toward.
2. `success_criteria` — the evidence that would prove the move worked.
3. `loop_status` — `one-shot 1/1` by default, or `iteration N/M`, `completed`, etc.
4. `resume_condition` — what is needed before the loop can continue.
