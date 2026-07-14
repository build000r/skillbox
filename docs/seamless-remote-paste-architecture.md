# Seamless remote paste architecture decision

Date: 2026-07-14
Status: accepted for implementation
Decision owner: Skillbox

## Decision

**BUILD** a small Skillbox-owned smart-paste router around the already-supported
Ghostty and tmux extension seams. **BORROW** containment, authentication, and
test ideas from `cc-clip` and image-path paste behavior from Codex and
`agenc-core`. Do **not** adopt `cc-clip`'s Codex X11 bridge as shipped.

The primary path is:

1. Ghostty maps `Cmd+V` to a private terminal sequence only in the managed
   configuration.
2. The outer local tmux maps that sequence to `User199` and captures the exact
   client and pane that received it.
3. The local router takes one typed clipboard snapshot on that explicit
   gesture.
4. Ghostty chains its native `paste_from_clipboard` action; the router observes
   text but never reinjects it, preserving Ghostty's native bracketed-paste and
   paste-protection behavior.
5. Images and supported files are atomically transferred to the registered
   remote route, then their remote path is sent back to that same captured
   pane as one bracketed-paste event.
6. Codex 0.144.4 recognizes a pasted, readable image path in
   `ChatComposer::handle_paste_image_path`, reads its dimensions, and creates a
   first-class `LocalImage` attachment. Other terminal programs receive the
   safe path as normal text.

`Ctrl+V` uses a second private sequence (`User198`) and the same route capture.
Because Ghostty does not chain native paste on that chord, the router injects
text exactly once as bracketed paste. `Cmd+V` remains the macOS-native default
and uses `User199` plus Ghostty's chained `paste_from_clipboard` action.

When `d2` or `d3` starts outside local tmux, the tracked launcher creates a
transparent disposable local tmux session and runs the registered route inside
it. This supplies the same exact interception point without changing the
remote shell or requiring the operator to manage tmux. In unrelated Ghostty
programs the unknown private CSI is ignored and Ghostty still performs its
native paste action. The explicit `clipimg-put` command remains the universal
recovery path. Codex desktop SSH projects are optional for new app-native
tasks, not a replacement for an already-running CLI or tmux session.

## Why this path

It is the only tested design that preserves all four non-negotiables at once:
one familiar gesture, exact focused-pane ownership, no clipboard polling, and
no unauthenticated listener. It also turns the same remote path into a native
Codex attachment without changing Codex or requiring a fake X server.

## Spike evidence

### Ghostty and local tmux: GO

Live versions were Ghostty 1.3.1 and tmux 3.6a on macOS. Ghostty's
`+validate-config` accepted:

```text
keybind = super+v=text:\x1b[99~
keybind = chain=paste_from_clipboard
```

A disposable tmux server configured a high-numbered user key to the same private
sequence and bound it to a marker action. A real attached PTY received
the bytes and wrote the marker while reporting the exact client and pane:

```text
handled/dev/ttysNNN %0 spike
```

No production Ghostty or tmux binding was modified. The proof server, socket,
and temporary config were removed. This establishes the input seam; end-to-end
clipboard and remote-path proof is owned by the implementation closeout.

### `cc-clip` v0.9.1: BORROW, do not adopt Codex mode

Inspected upstream release and source:

- repository: `github.com/ShunmeiCho/cc-clip`
- license: MIT
- tag: `v0.9.1`
- release commit: `757e2a31a769ecf7836e211112c7a570453249ad`
- Darwin arm64 release archive checksum from upstream `checksums.txt`:
  `ec0b4d383f39c75d21d8e7c7a6ccbf8c670fa6a2090bb72ebd6d511d91510c61`
- extracted binary checksum used in the live spike:
  `06db86c71dec3a6f932e4ec68eaac697fbf5032b0377c8053538968fa9efe1f7`

Files inspected included `LICENSE`, `go.mod`, `README.md`,
`internal/daemon/server.go`, `internal/tunnel/fetch.go`, `internal/token/`,
`internal/setup/sshconfig.go`, `internal/service/launchd.go`,
`internal/shim/`, `internal/x11bridge/`, `internal/xvfb/xvfb.go`, their tests,
CI, release workflow, and upgrade documentation. Useful borrowed properties
include loopback rejection, constant-time bearer checks, mode-0600 tokens and
artifacts, atomic token replacement, a 20 MiB image cap, random temporary
names, defensive timeouts, and race-enabled CI.

The pinned binary was installed to a versioned Skillbox-owned path and
`setup skillbox-portfolio-devbox --codex` completed against live `d3`. The
HTTP daemon bound only to `127.0.0.1:18339`, the SSH tunnel worked, notification
health passed, and the remote bridge claimed the X clipboard. The decisive
runtime socket inspection then found:

```text
0.0.0.0:6000  Xvfb.patched
[::]:6000     Xvfb.patched
```

The process command used `Xvfb ... -listen tcp`; the log also showed no
`.Xauthority`, and the bridge fell back to an unauthenticated X connection.
That violates `SRP-T05` and `SRP-T06`. Skillbox immediately ran the upstream
Codex uninstall, verified the X11 listener and `DISPLAY` marker were absent,
stopped launchd, restored byte-identical local and remote config backups,
removed the remote deployment and local package, and uninstalled the newly
added `pngpaste`. Pre/post SHA-256 hashes matched for `~/.ssh/config`, local
`~/.codex/config.toml`, and remote `.bashrc`, `.profile`, and
`.codex/config.toml`.

Upgrade policy: borrowed behavior is represented by local contract tests, not
an implicit moving dependency. Re-evaluate a later pinned release only if it
uses a Unix X socket or authenticated loopback-only display and passes the same
live socket audit. Rollback policy is fail-closed: stop the bridge first,
remove markers/hooks, restore recorded hashes, and do not call the route ready
until the old listeners are gone.

### Codex desktop SSH remote composer: optional, not the primary path

The current official remote-connections contract says the desktop app starts a
remote Codex app server through SSH, using the remote login shell, and operates
remote project files and commands. The current image-input contract supports
pasting or attaching images in the app composer. Live prerequisites passed for
`skillbox-portfolio-devbox`: the concrete SSH alias resolves, batch SSH works,
the login shell exposes Codex 0.144.4, and the intended remote repository is
readable.

This is a supported path for a **new app-owned task**. It does not attach to an
already-running tmux TUI, inherit that terminal's exact transient process
state, or make `Cmd+V` work in arbitrary shells. Therefore it cannot be the
universal default requested by this epic. No session-continuity claim is made
between a desktop task and an existing CLI TUI.

Primary references:

- <https://learn.chatgpt.com/docs/remote-connections.md#connect-to-an-ssh-host>
- <https://learn.chatgpt.com/docs/image-inputs.md>

### Local TUI to remote app-server: transport works; local paths do not

Codex 0.144.4 was tested end to end with a remote app-server bound to
`127.0.0.1:45144`, an SSH local forward, and a local 0.144.4 TUI connected via
`--remote`. The server was never public. Version parity, remote `/srv/repos`
working-directory selection, and WebSocket transport worked.

Passing the Mac-only image path
`/Users/operator/repos/skillbox/assets/og-card.png` produced the remote
rollout event:

```text
Codex could not read the local image at
`/Users/operator/repos/skillbox/assets/og-card.png`: No such file or directory
```

Source at tag `rust-v0.144.4` confirms that protocol `LocalImage` carries a
path and request serialization performs `std::fs::read(&path)` on the
app-server side. The spike server, SSH forward, and temporary files were then
removed and both listener checks passed.

Verdict: no-go as a direct clipboard solution. A future app-server client could
materialize bytes through the server filesystem API before sending
`LocalImage`, but the Skillbox smart-paste transfer already solves the same
problem for the user's existing terminal workflow. Do not add that patch until
measured demand justifies a second transport.

Primary references:

- <https://learn.chatgpt.com/docs/app-server.md>
- `openai/codex` tag `rust-v0.144.4`, especially
  `codex-rs/protocol/src/user_input.rs`,
  `codex-rs/protocol/src/models.rs`, and
  `codex-rs/tui/src/bottom_pane/chat_composer.rs`

## Placement and ownership

The capability belongs in the existing Skillbox repository because Skillbox
already owns clipboard bootstrap, host profiles, tmux fragments, installed
operator helpers, and closeout receipts. Host launchers consume the registry;
they do not become a second routing authority. `agenc-core` remains a consumer
and pattern source, not the system of record.

The implementation must remain stdlib-first and dependency-light. A reusable
SSH channel may be introduced only if real p95 measurements miss the contract;
correctness and focus ownership take precedence over speculative latency.

## Post-proof hardening

The source implementation now asks the remote receiver to delete an exact,
newly-created content-addressed artifact when clipboard generation, route
generation, focus ownership, or pane injection fails after transfer. It never
deletes a reused artifact that may back an earlier reference. The receiver
validates the digest and extension, locks the private store, refuses symlinks
or unowned files, and returns a bounded receipt. This closes the uncommitted
artifact portion of `SRP-T11` through `SRP-T13`.

This hardening landed after the recorded `devbox-1` proof. Under the operator's
session-safety restriction it has not been redeployed to the shared remote
home, so the live proof document remains evidence for the earlier receiver and
does not claim source/install parity for this later change.
