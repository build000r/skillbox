# Seamless remote paste closeout audit — 2026-07-14

Verdict: **not eligible for epic closure yet**. The owned core implementation
is present and the permitted `devbox-1` semantic proof passed, but required
physical-key, direct d3/mosh, full text-matrix, sample-size, source/install
parity, origin-clone, cross-platform, and independent-review evidence
is incomplete. No open Bead below is silently counted as complete.

Operator boundary: after the explicit restriction, no other remote tmux
session may be listed, attached, signaled, reloaded, or written. Offline/local
fixtures are allowed. The shared remote receiver was not updated after the
post-proof deletion hardening because doing so would change a helper used by
neighboring sessions.

## Evidence that is green

| Requirement area | Evidence | Verdict |
|---|---|---|
| Product contract and threat model | `docs/seamless-remote-paste.md` | Proven at design/contract level |
| Architecture/adopt gate | `docs/seamless-remote-paste-architecture.md`; cc-clip X11 exposure rejected and removed | Proven |
| Typed capture | macOS AppKit plus experimental Linux/Windows substrates; byte/type/dimension/race tests | Proven for macOS source; other platforms experimental |
| Route identity | Tracked d2/d3 launchers, generation-stamped records, exact pane/client checks | Proven by source/tests and `devbox-1` route record |
| Secure artifact transport | Atomic content-addressed receiver, hash/size/path/mode checks, quota/TTL/dedupe, exact canceled-artifact deletion, private single-link lock validation | Proven by adversarial tests; latest receiver revisions not remote-deployed |
| Agent adapter | Codex remote readable path decision plus Claude/generic fallback | Proven; live `devbox-1` showed `[Image #1]` and semantic confirmation |
| Feedback | 500 ms delayed notice, stable path-free public errors, retry/doctor, cancel, private `0600` diagnostic receipts | Proven by deterministic hostile tests |
| Lifecycle | Exact implementation commit `a6981014e8b505ed519942db73c4989bd92dce14` was cloned with `--no-local --no-hardlinks` into an empty fixture; install was ready, the second install was byte-identical, route state was correctly `ambiguous` without focus, no target probe ran, and uninstall left zero files/symlinks | Proven from a fresh local clone; origin/push remains a release gate |
| Diagnostics | Live local listener discovery, private modes, route freshness, duplicates, redacted target/session/HOME/receipt output, fail-closed manifest parsing | Proven locally |
| Partial failure | Remote apply/reversal errors suppress hostile output, state which side may be partial, and print one exact idempotent resume command | Proven with fully stubbed SSH |
| Focused regression | 203 passed, one opt-in live clipboard observer skipped; Ruff/Bash/diff clean | Proven |
| Offline closeout | `20260714T210526Z-smoke`: unit/static/bootstrap PASS; all live rows explicit SKIP | Proven as smoke only |
| Beads lint | All currently open/deferred plan issues lint with zero warnings | Proven |

## Open core Beads and exact missing proof

| Bead | Implemented evidence | Missing evidence / release action |
|---|---|---|
| `.2.4` latency optimization | Two redacted image receipts: p50 881.395 ms, p95 1007.121 ms | At least 20 authorized warm samples; only then decide whether SSH connection reuse is justified |
| `.4.1` one-key router | Installed Ghostty key list/config, tmux User198/User199 seam, idempotent lifecycle | One real human `Cmd+V`/`Ctrl+V` on the permitted route; synthetic AppleScript is explicitly rejected |
| `.4.2` text semantics | Native Cmd+V chain, exact bracketed Ctrl+V, multiline/metacharacter fixtures | Real Ghostty checks across every required transport; other sessions/hosts currently out of bounds |
| `.4.3` exact-pane injection | Deterministic focus/clipboard/route races and live nested `devbox-1` attachment | Required direct d3 proof and latest receiver parity |
| `.4.4` feedback | Implementation and tests complete | Tracker waits on `.4.3`; close afterward without bypass |
| `.5.1` owned Codex bridge | BUILD path and live nested Codex attachment | Required SSH/mosh/reconnect/reattach rows and latest receiver deployment |
| `.5.5` safe degradation | Native text fallback, adapter fallback, typed errors, doctor checks | Tracker waits on `.5.1`; live missing-receiver/reconnect behavior remains to record |
| `.6.1` one-command bootstrap | Fresh committed clone reaches local ready state; byte-idempotent repeat install | Latest source receiver must be installed through an explicitly authorized route; repeated current-version remote proof |
| `.6.2`, `.6.3`, `.6.4` lifecycle/status/docs | Fail-closed fixed-path rollback, byte-idempotent reinstall, redacted status/error envelopes, and local evidence complete | Dependency-ordered behind `.6.1`; do not force-close |
| `.7.2` adversarial proof | Comprehensive deterministic source coverage, including lock inode, lifecycle manifests/backups/destinations, route/receipt schemas, path races, and public-error redaction | Dependency-ordered behind `.4.3` and `.5.1`; independent hostile case remains part of final review |
| `.7.3` real d3 + devbox-1 | `devbox-1` semantic/hash/clipboard proof | Direct d3 is required and unclaimed |
| `.7.4` full text matrix | Offline and smoke evidence | Real Ghostty SSH/mosh/nested/direct matrix, without disturbing unrelated sessions |
| `.7.5` stability/latency | Reconnect generation, concurrency, dedupe, lifecycle tests; two receipt samples | Rollout-size authorized sample and live reconnect/reattach evidence |
| `.7.6` fresh clone | Exact local clone of `a6981014e8b505ed519942db73c4989bd92dce14` reported install-ready/route-ambiguous without focus, performed a byte-identical second install, made no target probe, and uninstalled with zero files or symlinks | Push, prove origin contains the commits, then clone from origin; remote/fallback rows remain dependency-gated |
| `.7.7` independent review | This self-audit prepares the packet | A genuinely independent reviewer must verify artifacts after all live rows pass |

## Expansion Beads

| Bead | Current state | Missing for closure |
|---|---|---|
| `.8.1` JPEG/HEIC/PDF/Finder/multiple | Format fixtures and safe Finder/multi-item native fallback implemented | Live non-PNG samples and metadata/fallback observation |
| `.8.2` outside local tmux | d2/d3 create a transparent disposable local tmux boundary | Real direct Ghostty SSH and mosh key proof |
| `.8.3` Linux/Windows clients | `wl-paste`, `xclip`, and STA PowerShell capture substrate plus honest matrix | Platform-specific focus-safe installer/uninstaller and real image/native-text proof on each OS |
| `.8.4` adjacent ingress | Bounded report ranks drag/drop, mobile, and history | Tracker intentionally waits on independent core closeout |

## Required next authorization

The smallest safe live step is one manual paste into the already-existing
`devbox-1` Codex surface. The operator copies the known proof image, focuses
that exact surface, presses `Cmd+V` once, does not press Enter, and reports
whether `[Image #1]` appeared. No automation will observe even that pane until
the operator explicitly authorizes it; server enumeration remains forbidden.
Any direct d3, mosh, remote-helper update, or other-host matrix needs separate
permission because it falls outside the current session boundary.
