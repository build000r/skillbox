# Seamless remote paste performance — 2026-07-14

Scope: read-only aggregation of local smart-paste receipts. The command did not
read the clipboard, initiate a transfer, or contact any host.

```bash
scripts/clipboard-metrics
```

The current local receipt set contains two successful `image_path` samples
across two route generations: minimum/p50 881.395 ms, p95/maximum 1007.121 ms.
Both are under the 1.5 s p95 budget, but the observed p50 is above the 500 ms
target and two samples are far below the 20-sample rollout floor. One of the
samples is the exact `devbox-1` proof documented alongside this report; this
aggregate does not promote the other receipt to semantic live proof.

No reusable channel or daemon is justified from this sample set yet. The
existing path has no listener, is observable and reversible, and its valid
proof completed before progress would become intrusive. The latency-optimization
Bead stays open until at least 20 authorized warm gestures can be measured on
the permitted route. If p50 remains over 500 ms, reopen design work around an
SSH-owned connection reuse mechanism; never trade exact focus ownership or
listener containment for speed.

The metrics command outputs only counts, outcomes, route cardinality, latency
percentiles, and budget booleans. It omits route IDs, hosts, paths, clipboard
metadata, text, bytes, and credentials.
