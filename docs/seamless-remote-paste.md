# Seamless remote paste contract

Status: normative product and security contract. Implementations may add more
capabilities, but they must not weaken the behavior or threat controls below.

This contract replaces the operator-facing `clipimg-put <host>` workflow as the
daily path. The operator copies content, focuses an existing terminal or agent
composer, presses one familiar paste chord, and keeps working. Host selection,
transport, conversion, upload, and agent attachment are implementation details.

## Vocabulary

- **Paste gesture**: one explicit `Cmd+V` or `Ctrl+V` keypress. It is the only
  event that authorizes reading image or file bytes from the Mac pasteboard.
- **Route**: a registered, generation-stamped mapping from a focused local
  terminal or tmux pane to the exact remote host, hop chain, transport, remote
  tmux pane, home directory, and attachment capabilities.
- **Snapshot**: an immutable typed description of the pasteboard at gesture
  time, including change count, type, byte size, and digest. Receipts never
  contain clipboard bytes.
- **Native attachment**: the remote agent displays an image attachment, not
  merely shell text naming a file.
- **Reference fallback**: a contained remote path is pasted into the exact
  focused pane when the agent cannot accept a native attachment.
- **Fail closed**: text paste remains available, but image/file bytes are not
  uploaded or injected when target identity, authorization, or focus is unsure.

## Non-negotiable operator journey

1. Copy text, an image, or a supported file on the Mac.
2. Focus the already-running local or remote terminal/agent surface.
3. Press one configured paste gesture.
4. Text behaves exactly like native bracketed paste. A supported image becomes
   one visible agent attachment or one safe remote reference in the same pane.
5. Continue typing. The bridge never sends Enter and never replaces the Mac
   clipboard.

The normal journey has no helper command, host selector, second paste,
confirmation dialog, shell evaluation, session recreation, or manual tunnel.

## Chord ownership

`Cmd+V` is the universal Ghostty/tmux smart-paste gesture on macOS. It preserves
native text semantics and dispatches non-text snapshots only for a registered
route. Managed Ghostty also maps terminal-first `Ctrl+V` to the same exact-pane
router; on that chord the router performs one bracketed text paste itself.
Both chords obey the same snapshot, route, race, privacy, and fallback rules.
A pane without a complete route/generation never invokes the clipboard helper:
`Cmd+V` stays on Ghostty's native action and `Ctrl+V` sends its original literal
`^V` byte to the pane.
A profile must advertise which chord it owns; the installer must not shadow an
unrelated application or global macOS paste binding.

## Normative clipboard behavior

| Clipboard state | Registered supported route | Unknown, stale, or ambiguous route | Operator-visible result |
|---|---|---|---|
| Plain or rich text | Use the terminal's native bracketed paste path; do not upload | Use native text paste | Text appears once; no Enter |
| Multiline or shell metacharacters | Use native bracketed paste without evaluation | Use native text paste | Exact bytes are pasted once |
| PNG or TIFF image | Prefer native attachment; otherwise upload once and paste one contained reference | Do not read image bytes; show bounded unsupported-route feedback | Attachment/reference appears in the originally focused pane |
| Supported file URL | Negotiate attachment or contained upload; preserve a safe display name | Do not upload; preserve ordinary path/text paste when the OS supplied text | One attachment/reference, never shell execution |
| Empty clipboard | Perform no transfer or injection | Perform no transfer or injection | Quiet no-op or one bounded notice |
| Unsupported media | Perform no transfer or injection; identify the unsupported type | Perform no transfer or injection | One bounded notice and exact fallback action |
| Oversized or corrupt media | Reject before transport or quarantine validation | Reject | One bounded error; no partial remote artifact |
| Offline target | Do not inject a speculative path; permit explicit retry | Same | Existing prompt remains untouched |
| Focus changes in flight | Cancel and remove any uncommitted artifact | Cancel | Nothing appears in either pane |
| Clipboard changes in flight | Cancel the old generation; a new gesture is required | Cancel | New clipboard content is never sent by the old gesture |

Image/file reads are gesture-triggered. Text may continue through the terminal's
native paste implementation without being copied into Skillbox diagnostics.

## Surface acceptance matrix

Every **required** row is a release-blocking live proof. Optional rows must be
labeled honestly and may not be counted toward the core closeout verdict.

| Surface | Route identity | Transport | Text expectation | Image expectation | Requirement |
|---|---|---|---|---|---|
| Local Ghostty + local tmux | Local client and pane IDs | Local PTY | Native bracketed paste | Smart gesture dispatches without changing other apps | Required |
| Direct d3 | Registered `d3` launcher/session generation | SSH | Native paste/OSC52 behavior unchanged | One gesture creates a visible Codex attachment or safe reference | Required |
| d2 to devbox-N | Registered outer d2 pane plus exact devbox ID | SSH hop chain | Native paste unchanged | One gesture reaches only the selected devbox | Required |
| Nested local + remote tmux | Local client/pane and remote session/window/pane tuple | SSH + tmux | Exact bytes arrive once | Attachment/reference reaches the originally focused remote pane | Required |
| Direct remote tmux | Registered remote tmux tuple | SSH | Exact bytes arrive once | Same as nested path without outer hop | Required |
| d3 over mosh | Registered mosh session plus owned SSH sidecar | Mosh + loopback SSH tunnel | Native paste/OSC52 behavior unchanged | Native attachment or reference survives reconnect | Required |
| Conference1 direct WSL | Registered direct WSL route | SSH or mosh | Native paste/OSC52 behavior unchanged | Same fail-closed image semantics | Required |
| Generic SSH host | Explicit allowlisted profile and route generation | SSH | Native text paste | Image only when capability and trust handshake pass | Optional |
| Codex native bridge | Exact Codex process environment and bridge generation | SSH reverse tunnel/X11 bridge | Codex text behavior unchanged | `Ctrl+V` displays a native image attachment | Required when selected by ADR |
| Codex desktop SSH composer | Codex app remote project identity | Official SSH remote connection | Local composer paste | `Cmd+V` displays an image input | Optional until live spike selects it |
| Local TUI + remote app-server | Versioned app-server connection | Loopback/Unix socket through SSH | Local TUI semantics | Image bytes must materialize remotely, not remain a Mac-local path | Experimental |
| Linux Wayland/X11 operator | Exact local tmux pane and tracked route | SSH or mosh | Native terminal semantics must be proven | Typed `wl-paste`/`xclip` substrate exists | Experimental; not a core pass |
| Native Windows operator | Stable terminal surface ID not yet implemented | SSH | Native terminal semantics must be proven | STA PowerShell capture substrate exists | Unsupported until a scoped installer and live proof exist |

## Latency and feedback budget

Latency is measured from key-down to visible attachment/reference or definitive
failure feedback, on a warm registered route with an image no larger than 5 MiB.

| Measurement | Budget | Required behavior |
|---|---:|---|
| Text paste p50 / p95 | no more than 10 ms / 30 ms added router overhead | Indistinguishable from native paste |
| Warm image paste p50 | at most 500 ms | No progress UI required |
| Warm image paste p95 | at most 1.5 s | Quiet progress appears after 500 ms |
| Definitive local rejection | at most 150 ms | One bounded reason; no prompt injection |
| Cancel after focus/clipboard race | at most 100 ms after detection | No target receives content |
| Offline/auth timeout | at most 3 s by default | Exact retry/repair action, no speculative path |

Measurements record content size, transport, route, cold/warm state, and each
component duration. A reusable channel is justified only when the measured
baseline misses these budgets.

## Fallback and recovery semantics

The ordered capability choice is:

1. Native attachment in the focused agent.
2. Secure contained upload plus one agent-recognized remote reference.
3. Explicit `clipimg-put` recovery instructions initiated by the operator.
4. No transfer.

Fallback never silently widens the host allowlist, changes transport trust,
targets a neighboring pane, exposes a listener, sends Enter, or converts an
image to text. Image failure must not break ordinary text paste or shell startup.

Success may be quiet. Failures use one stable error code and one repair action;
they do not inject diagnostics into the terminal input stream. Retry is explicit
unless it can reuse the same route and clipboard generations without ambiguity.

## Audience outcomes

| Audience | Required outcome |
|---|---|
| Operator | One familiar gesture works in the existing remote session; the clipboard, focus, and prompt remain under operator control |
| Agent | Receives one valid attachment/reference with stable type and digest metadata; never receives partial bytes or an accidental Enter |
| Maintainer | Can inspect route/capability state, reproduce deterministic failures, rotate credentials, roll back/uninstall, and prove every core surface |

## Security invariants

1. Clipboard image/file bytes are read only after an explicit paste gesture.
2. Listeners bind to loopback or an owned Unix socket; no clipboard service binds
   to `0.0.0.0` or a Tailnet/public interface.
3. Routes are explicit, allowlisted, generation-stamped, and revalidated after
   asynchronous work. Window titles alone are never routing authority.
4. Tokens are random per-operator secrets, stored mode `0600`, compared in
   constant time, absent from argv/logs/receipts, rotatable, and revocable.
5. Remote artifacts remain inside an owned mode `0700` root; files are created
   without following symlinks, with unpredictable names and mode `0600`.
6. MIME signatures, decode validity, per-item and aggregate size limits, and
   timeouts are enforced before an adapter sees content.
7. A transfer binds token, route generation, clipboard generation, digest,
   nonce, and expiry. Replay cannot create a second injection.
8. Concurrent requests cannot cross routes or panes. Commit-time focus and
   clipboard generation checks decide whether injection is still authorized.
9. Receipts contain metadata only. Clipboard bytes, token values, full sensitive
   paths, and pasted text never enter logs.
10. Partial, expired, canceled, and unreferenced artifacts are removed by a
    bounded lifecycle job. Cleanup never follows attacker-controlled links.
11. Multi-user remote hosts are unsupported unless the profile explicitly opts
    in with a per-user bridge, token, runtime directory, and permissions proof.
12. Uninstall removes only Skillbox-owned bindings, services, tokens, routes,
    and artifacts and restores the prior owned configuration.

## Threat model and test ownership

| ID | Threat | Required prevention | Verification owner |
|---|---|---|---|
| SRP-T01 | Clipboard-change surveillance or polling | Read non-text pasteboard bytes only inside explicit gesture handling; no background clipboard history | Clipboard snapshot unit tests + macOS live observer |
| SRP-T02 | Wrong focused pane receives content | Bind the gesture to stable local/remote pane IDs; revalidate focus and route generation immediately before injection | Route contract tests + real concurrent-pane proof |
| SRP-T03 | Stale route after reconnect/reattach | Expiring generation-stamped registration and capability handshake; fail closed on mismatch | Session registry tests + reconnect live proof |
| SRP-T04 | Tunnel hijack or port collision | Loopback bind, SSH-owned forwarding, authenticated request envelope, random available endpoint, ownership check | Transport security tests |
| SRP-T05 | Non-loopback clipboard service exposure | Reject startup/status readiness when bind address is not loopback/Unix; runtime exposure lint | Doctor tests + socket inspection in live proof |
| SRP-T06 | Token theft through permissions, argv, or logs | Mode `0600`, stdin/file descriptor transport, redaction, rotation, no environment dump | Auth tests + process/log inspection |
| SRP-T07 | Symlink/path traversal escape | Owned mode `0700` root, dirfd-relative no-follow creation, server-generated names, canonical containment check | Artifact-store adversarial tests |
| SRP-T08 | Oversized, corrupt, polyglot, or decompression-bomb media | Byte and decoded-dimension caps, signature/decode validation, bounded conversion, timeout | Media validation tests |
| SRP-T09 | Replay or duplicate injection | Nonce, expiry, digest and route binding, atomic consume-once receipt | Protocol replay/concurrency tests |
| SRP-T10 | Concurrent sessions cross-deliver | Per-route queues and tokens, immutable request context, commit-time identity recheck | Multi-session stress tests |
| SRP-T11 | Focus changes during upload | Cancel before injection; remove uncommitted artifact; require a new gesture | Deterministic race test + live focus-switch proof |
| SRP-T12 | Clipboard changes during upload | Compare pasteboard change count/digest before commit; never substitute newer bytes | Deterministic race test |
| SRP-T13 | Partial transfer or crashed client leaves sensitive files | Atomic temp-to-final rename, TTL ledger, startup reconciliation, bounded cleanup | Fault-injection and lifecycle tests |
| SRP-T14 | Malicious filename or shell content executes | Server-generated storage name, separate escaped display name, argv arrays, bracketed paste, never auto-Enter | Filename corpus + PTY tests |
| SRP-T15 | Untrusted or multi-user host observes clipboard | Explicit allowlist/capability trust; per-user opt-in and permissions; no generic guessing | Profile lint + multi-user negative test |
| SRP-T16 | Diagnostics leak content or credentials | Structured redacted receipts with type/size/hash/timing only | Golden schema and secret-scan tests |
| SRP-T17 | Dependency/update compromise | Pinned version/checksum, provenance record, rollback, compatibility tests, upstream diligence | Bootstrap/update tests + release review |
| SRP-T18 | Uninstall breaks normal paste or deletes user state | Scoped owned config markers and byte-equivalent restore fixtures | Install/uninstall round-trip tests |

Every implementation issue must cite the relevant `SRP-Txx` IDs in tests or
closeout evidence. Security-sensitive failure is release-blocking; an
unsupported optional surface must be reported as unsupported, not silently
counted as a pass.

## Proof record

Core live proof uses a nonce-bearing image with distinctive visual content. It
records the source digest and pasteboard change count, route and focus
generations, selected adapter, delivered digest, component timings, visible
agent result, cleanup result, and final clipboard equality. Synthetic SCP proof
is transport evidence only and cannot satisfy the one-gesture acceptance row.

The closeout reviewer must reject completion when a required row is skipped,
when only source-level fixtures exist, or when the live host still uses a legacy
untracked launcher/configuration path.
