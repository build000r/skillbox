# Oracle latency and reliability metrics

`runtime_manager.oracle_metrics` is the operator view of the Oracle subagent:
how long runs take, how often they succeed, where the failures land, and how
deep the queue is. It is a library module in the same shape as
`runtime_manager.oracle_policy` — no CLI verb, no MCP tool, no listener.

The Oracle lane handles prompts, private research URLs, browser profiles,
cookies, and account identity. None of that may reach a dashboard, a log line,
or a persisted metrics document. This contract does not redact metrics; it
makes a leak structurally impossible.

## Why there is nothing to redact

Every field is a bounded integer, a bool, or a token from a closed vocabulary
declared in the module. There is no message field, no URL field, no path field,
and no caller field, so there is nothing for sensitive text to ride in on.

The only variable-shaped strings in the whole contract are:

- `run_id` — an opaque 32-hex CSPRNG value minted by `new_run_id()`, generated
  rather than derived, so it carries no prompt, caller, or URL entropy;
- `result_digest` — a 64-hex SHA-256 of the delivered bytes;
- rendered UTC timestamps, `YYYY-MM-DDTHH:MM:SS.mmmZ`.

Each is pinned to an exact regex. Before any document leaves the module,
`assert_emission_safe()` re-walks it and rejects any key outside the structural
key set and any string that is neither a vocabulary token nor one of those three
shapes. That check is deliberately redundant with construction: it turns "we
believe the schema is closed" into an enforced invariant, so a field added later
without a vocabulary entry fails closed instead of publishing whatever the
caller passed in.

`tests/test_oracle_metrics.py` proves this from both ends. Every string-shaped
input is fed prompt text, private URLs, an operator handle, a home-directory
cookie path, an API key, a bearer token, a session cookie, and a Tailscale
authkey, and each must raise `OracleMetricsError`. Every emitted document is
then walked so that a string outside the declared vocabulary is a test failure,
and the rendered bytes are asserted to contain no `http`, `@`, `/Users/`,
`Bearer`, `cookie`, or `token` substring at all.

### Deliberate non-goal: per-caller metrics

No caller ID, tenant, or session identifier is recorded. Per-caller reliability
would require exactly the account identity this lane must not expose, and a
salted digest of it would still be a stable correlation handle. Runs correlate
by their own `run_id` only. If per-caller reliability is ever genuinely needed,
it belongs behind the policy engine's authenticated surface, not here.

## Vocabulary

Terminal run states — a sample is recorded only for a finished run; live
progress is carried by the queue gauges instead:

| State | Meaning |
| --- | --- |
| `completed` | delivered a nonempty result |
| `failed` | died in the lane |
| `denied` | refused by policy before browser contact |
| `timed_out` | submitted, never finished |
| `cancelled` | client went away |

Stage reached — how far through the lane the run got, in order: `queued`,
`admitted`, `staged`, `browser_ready`, `submitted`, `generating`, `delivered`.
This is the "where did it die" signal that makes a failure count actionable
without any run content.

Error classes are classes, never messages: an exception string could quote a
URL, a prompt fragment, or a filesystem path. The vocabulary is `none`,
`policy_denied`, `quota_exceeded`, `attachment_rejected`, `browser_unavailable`,
`browser_crashed`, `auth_expired`, `navigation_failed`, `submit_failed`,
`response_timeout`, `result_empty`, `transport_error`, `client_cancelled`, and
`internal_error`.

Each terminal state accepts a disjoint subset of those classes, and each state
constrains which stages it may claim to have reached. A `completed` run must
report `none` and `delivered`; a `denied` run must report a policy class and
cannot claim it ever reached the browser. The partition is verified at import
and again by the tests, so the vocabulary cannot quietly stop being closed.

Latency phases: `queue_wait`, `admission`, `browser_acquire`,
`attachment_stage`, `submit`, `first_output`, `generation`, `result_write`,
`total`. `total` is mandatory on every sample; the rest are optional, because a
run that died at admission never had a submit phase. No individual phase may
exceed `total` — phases may overlap or be sampled independently, so their sum is
not a meaningful bound, but a single phase longer than the whole run is a bug.

## Run-bound evidence

The failure this contract exists to prevent is a completed receipt with nothing
behind it. `OracleRunSample` therefore refuses to construct a `completed` run
unless it carries `result_bytes >= 1` and a well-formed SHA-256
`result_digest` of the bytes that were actually written, and it refuses any
non-completed run that claims either. Emit the sample after the atomic result
write, digesting what landed on disk — not what was expected to land.

Phase durations come from `PhaseTimer`, which reads `time.monotonic_ns()` only.
Wall-clock deltas can go backwards across an NTP step and would turn a latency
panel into fiction. The clock is injectable for tests; a backwards reading, a
non-integer reading, a double `start()`, an orphan `stop()`, or calling
`durations()` while a phase is still open all fail closed.

## Reading a snapshot

`OracleMetricsRegistry` holds a bounded ring buffer of samples (512 by default)
plus the latest queue gauges. `snapshot()` renders the operator view and
`render()` returns its canonical bytes — sorted keys, minimal separators, ASCII,
one trailing newline, matching `oracle_policy`'s persistence discipline so
documents stay byte-comparable across hosts and Python versions.

Every enumerated bucket is present and zero-filled regardless of traffic, so the
shape does not change the first time a rare error class appears.

```json
{
  "schema": "skillbox.oracle-metrics-snapshot.v1",
  "generated_at": "2027-01-15T08:07:00.000Z",
  "window": {
    "samples": 6,
    "capacity": 512,
    "oldest_at": "2027-01-15T08:00:00.000Z",
    "newest_at": "2027-01-15T08:05:00.000Z"
  },
  "gauges": {
    "queue_depth": 2,
    "queue_depth_max": 2,
    "inflight": 2,
    "capacity": 2,
    "observed_at": "2027-01-15T08:06:40.000Z"
  },
  "runs": {
    "total": 6,
    "warm": 3,
    "cold": 3,
    "by_state": {
      "completed": 3, "failed": 1, "denied": 1, "timed_out": 1, "cancelled": 0
    },
    "by_mode": { "standard": 5, "deep-research": 1 },
    "by_stage_reached": {
      "queued": 1, "admitted": 0, "staged": 0, "browser_ready": 1,
      "submitted": 0, "generating": 1, "delivered": 3
    }
  },
  "reliability": {
    "success_rate_ppm": 500000,
    "attempts_total": 9,
    "retried_runs": 2,
    "by_error_class": {
      "none": 3, "browser_crashed": 1, "quota_exceeded": 1,
      "response_timeout": 1, "policy_denied": 0, "attachment_rejected": 0,
      "browser_unavailable": 0, "auth_expired": 0, "navigation_failed": 0,
      "submit_failed": 0, "result_empty": 0, "transport_error": 0,
      "client_cancelled": 0, "internal_error": 0
    }
  },
  "latency_ms": {
    "queue_wait":       { "count": 6, "min": 0,     "p50": 8,     "p95": 140,     "max": 140 },
    "admission":        { "count": 6, "min": 2,     "p50": 2,     "p95": 3,       "max": 3 },
    "browser_acquire":  { "count": 5, "min": 190,   "p50": 210,   "p95": 9100,    "max": 9100 },
    "attachment_stage": { "count": 0, "min": null,  "p50": null,  "p95": null,    "max": null },
    "submit":           { "count": 4, "min": 1760,  "p50": 1790,  "p95": 2100,    "max": 2100 },
    "first_output":     { "count": 4, "min": 4800,  "p50": 5000,  "p95": 6100,    "max": 6100 },
    "generation":       { "count": 4, "min": 38200, "p50": 41300, "p95": 7200000, "max": 7200000 },
    "result_write":     { "count": 3, "min": 15,    "p50": 18,    "p95": 21,      "max": 21 },
    "total":            { "count": 6, "min": 3,     "p50": 45000, "p95": 7203000, "max": 7203000 }
  }
}
```

Reading rules that matter:

- **`success_rate_ppm` is parts per million, floored.** It is an integer so the
  canonical encoding stays byte-stable and no float formatting difference shows
  up as a fake metrics change. 500000 is 50%.
- **Percentiles are nearest-rank over the window, no interpolation.** With
  `count` below about 20 a p95 is just "the slowest one or two runs"; treat it
  as an anecdote, not an SLO reading.
- **A `count` of 0 renders as `null`, not 0.** A phase no run reached has no
  latency, and reporting 0 ms would read as "instant".
- **Latency buckets mix all terminal states.** In the snapshot above, `total`
  p95 is 7203000 ms purely because one run timed out — the successful runs
  finished in 45–67 s. Always read `latency_ms` next to `by_state` and
  `by_error_class`; a timeout or a `denied` run (`total` of 3 ms) distorts both
  tails. The registry deliberately does not pre-filter, because hiding the
  timeout from the latency panel is how a broken lane looks healthy.
- **`warm` versus `cold` explains `browser_acquire`.** The 9100 ms max above is
  a cold acquire that then crashed; warm acquires sit near 200 ms.
- **`retried_runs` and `attempts_total` separate flakiness from failure.** A run
  that succeeded on attempt 3 counts as `completed`, so a rising
  `attempts_total` against a flat failure count is the early warning.
- **`queue_depth_max` is a high-water mark since process start**, not a windowed
  value; `queue_depth` and `inflight` are the latest reading, stamped with
  `observed_at`.

## Non-goals

This module holds a bounded in-memory window. It is not a time-series database,
does not persist across restarts, does not export to a scrape endpoint, and
does not evaluate SLO thresholds. Those belong to whatever consumes
`render()`. The SLO targets themselves live with the benchmark harness in the
skills tree, not here.

## Tests

```
PYTHONPATH=.env-manager python3 -m unittest tests.test_oracle_metrics
python3 -m ruff check .env-manager/runtime_manager/oracle_metrics.py tests/test_oracle_metrics.py
```
