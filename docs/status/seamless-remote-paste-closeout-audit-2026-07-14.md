# Seamless remote paste closeout audit — 2026-07-14

Verdict: **not eligible for epic closure yet**. The owned core implementation
is present and the permitted `devbox-1` semantic proof passed, but required
physical-key, direct d3/mosh, full text-matrix, sample-size, source/install
parity, fresh committed-clone, cross-platform, and independent-review evidence
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
| Secure artifact transport | Atomic content-addressed receiver, hash/size/path/mode checks, quota/TTL/dedupe, exact canceled-artifact deletion | Proven by adversarial tests; latest delete revision not remote-deployed |
| Agent adapter | Codex remote readable path decision plus Claude/generic fallback | Proven; live `devbox-1` showed `[Image #1]` and semantic confirmation |
| Feedback | 500 ms delayed notice, stable errors, retry/doctor, cancel, private receipts | Proven by deterministic tests |
| Lifecycle | Empty-HOME install reached `ready`; uninstall restored tmux/Ghostty byte-exactly and left no managed files or bytecode | Proven locally |
| Diagnostics | Live local listener discovery, private modes, route freshness, duplicates, redacted status/metrics | Proven locally |
| Focused regression | 157 passed, one opt-in live clipboard observer skipped; Ruff/compile/Bash/diff clean | Proven |
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
| `.6.1` one-command bootstrap | Local install and earlier remote receiver install | Latest source receiver must be installed through an explicitly authorized route; repeated current-version remote proof |
| `.6.2`, `.6.3`, `.6.4` lifecycle/status/docs | Implementation and local evidence complete | Dependency-ordered behind `.6.1`; do not force-close |
| `.7.2` adversarial proof | Comprehensive deterministic source coverage | Dependency-ordered behind `.4.3` and `.5.1` |
| `.7.3` real d3 + devbox-1 | `devbox-1` semantic/hash/clipboard proof | Direct d3 is required and unclaimed |
| `.7.4` full text matrix | Offline and smoke evidence | Real Ghostty SSH/mosh/nested/direct matrix, without disturbing unrelated sessions |
| `.7.5` stability/latency | Reconnect generation, concurrency, dedupe, lifecycle tests; two receipt samples | Rollout-size authorized sample and live reconnect/reattach evidence |
| `.7.6` fresh clone | Empty-HOME source fixture | Commit/push, clone that exact commit, repeat install/update/uninstall/fallback from clone |
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
that exact surface, and presses `Cmd+V`; automation may observe only the exact
registered `devbox-1` pane and must not enumerate the server. Any direct d3,
mosh, remote-helper update, or other-host matrix needs separate permission
because it falls outside the current session boundary.
