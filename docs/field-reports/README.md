# Field reports — the voice-of-customer intake loop

This directory is the honest pipeline that turns **real** skillbox operators into
quotable, attributed proof. It exists so that when a testimonial eventually
appears on a marketing surface (README, website, OG card, social posts), it can
be traced back to a real human who actually said it and gave permission.

> **The covenant.** Quotes only appear on any skillbox marketing surface with
> the reporter's **permission**, their **handle**, and a **link** to the original
> report. No exceptions.

## The hard rule (P14 / P29)

Fabrication is prohibited. This is not a style preference — it is a principle.

- **P14 (copy stolen from customers):** the best marketing copy is the operators'
  own phrasing. We may only borrow language that a real operator actually wrote.
- **P29 (testimonials):** a testimonial must come from a real, captured, and
  permissioned field report. Inventing, paraphrasing-into-a-quote, or
  "representative" testimonials are all forbidden.

**No testimonial or quote may appear on a marketing surface until it comes from a
real captured report in this directory, with `permission_to_quote: yes`.** Until
at least one such report exists, marketing surfaces show *no* testimonial section
at all (no empty scaffolding, no "coming soon" begging wall).

## How a real user submits a field report

There are three equivalent front doors. All of them land here as a structured
entry.

1. **GitHub issue** — open a *Field report* issue on the repo (the new-issue
   chooser offers it) and answer the four prompts below.
2. **Helper script** — run [`scripts/field-report.sh`](../../scripts/field-report.sh),
   which walks you through the fields and appends a JSONL row to
   [`reports.jsonl`](reports.jsonl).
3. **Copy the template** — duplicate [`TEMPLATE.md`](TEMPLATE.md), fill it in, and
   attach it to an issue or PR.

## The four questions (the intake prompts)

These are the questions worth asking about skillbox specifically. Keep them
verbatim so answers stay comparable across reports:

1. **What did skillbox replace?**
2. **What survived a rebuild or restart?** *(the sharpest VoC question for a
   durable-box product)*
3. **Which command made the box feel real?**
4. **What still hurt?**

## Fields to capture

Every report — however it arrives — should end up with these fields:

| Field | Meaning |
| --- | --- |
| `date` | ISO date the report was captured (`YYYY-MM-DD`). |
| `source` | Where it came from: `github-issue`, `script`, `email`, `dm`, etc. |
| `handle` | The reporter's public handle for attribution (e.g. `@octocat`). |
| `link` | Link to the original report (issue URL, gist, message permalink). |
| `context` | Who they are / their setup / what they were doing. |
| `quote` | The exact words, verbatim. Do not clean up or paraphrase. |
| `permission_to_quote` | `yes` / `no` — may this be shown publicly? |
| `answers` | The four-question answers (optional but encouraged). |

A quote is **eligible for a marketing surface only if** it has all three of
`permission_to_quote: yes`, a non-empty `handle`, and a non-empty `link`.

## When quotes arrive

Once one or more eligible reports exist:

1. Add a "What operators reported" section to the README, showing only
   eligible quotes with their handle + link.
2. Mine the reports for P14: start borrowing the operators' actual phrasing in
   headlines and section copy.

Until then: this loop stands ready and empty. That is the correct state.
