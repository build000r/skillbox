---
name: loop
description: Run a prompt or slash command on a recurring interval (e.g. /loop 5m /foo). Omit the interval to let the model self-pace. Use when the user wants to set up a recurring task, poll for status, or run something repeatedly on an interval.
---

# Loop

Run a prompt or slash command on a recurring interval, or let the model
self-pace iterations when no interval is given.

## Read-before-Edit Invariant

Before calling Edit on any file, you MUST first call Read on that file in this
conversation. The Edit tool will reject calls on files that have not been Read.
Never assume file contents from memory or prior sessions — always Read first,
then Edit. This applies at every iteration of the loop: even if you edited a
file in a previous iteration, you must Read it again before the next Edit.
Skipping Read wastes tool calls on retries and breaks the edit cycle.

## Loop Execution

1. Parse the user's request: extract the interval (if any) and the prompt or
   slash command to repeat.
2. On each iteration, execute the prompt or skill.
3. Between iterations, respect the specified interval or self-pace based on
   the nature of the work.

## File Modification Rules

When a loop iteration involves modifying files:

- **Always Read before Edit.** Every file you intend to Edit must be Read first
  in the current iteration. Prior iterations do not count — the file may have
  changed.
- **Verify before modifying.** Check that the file still contains what you
  expect before applying an edit.
- **One concern per iteration.** Keep each iteration focused on a single
  coherent change to avoid cascading failures across loop cycles.

## Self-Pacing

When no interval is specified, choose the next iteration delay based on:

- The nature of the task (polling vs. iterative work)
- Whether the previous iteration made progress
- Whether external state needs time to change

## Termination

The loop ends when:

- The success criteria from the prompt are met
- The user cancels
- No further progress can be made
- The iteration budget is exhausted
