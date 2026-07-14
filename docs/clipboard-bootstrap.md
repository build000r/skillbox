# Clipboard bootstrap

Skillbox owns one-gesture text/image paste and OSC52 copy for operator Mac +
Ghostty, local tmux, SSH/mosh remotes, nested tmux, and Conference1 WSL. Source bundle:
`scripts/clipboard/`. Bootstrap entry: `scripts/clipboard-bootstrap`.

## Daily path: copy, focus, paste

After bootstrap, copy a screenshot or image, focus the existing `d2`/`d3`
terminal, and press `Cmd+V` (or terminal-first `Ctrl+V`). There is no host
argument, helper command, second paste, or Enter. Text stays on Ghostty's
native bracketed-paste path for `Cmd+V`; `Ctrl+V` uses the same exact-pane
router. For an image, the router uploads bounded media bytes and injects a
readable remote path into the exact pane; the proven Codex adapter renders that
reference as a visible attachment. This is reference injection, not raw image
bytes traveling through the terminal paste protocol.

The launcher registers the exact local pane/client, canonical host, transport,
and `devbox-N` generation. Image transfer refuses unknown or stale routes.
`d2`/`d3` started outside tmux create a disposable local tmux boundary so the
operator does not need to manage one.

## Prerequisites

The remote profiles in `scripts/clipboard/hosts.json` use **SSH aliases, not
raw IPs**. Profiles assume that `skillbox-jeremy-3` and `conference1-wsl`
(plus `sweet-potato-prod` and `skillbox-portfolio-devbox`) resolve via
`~/.ssh/config` on the machine running the bootstrap. Without those Host
blocks, `--profile jeremy` / `--profile conference1` (and `clipimg-put j|c`)
cannot connect.

Sample Host blocks:

```ssh-config
Host skillbox-jeremy-3
    HostName 100.105.106.104
    User skillbox

Host conference1-wsl
    HostName 100.96.206.87
    User worker
    IdentityFile ~/.ssh/id_ed25519_conference1
    IdentitiesOnly yes
```

Conference1 direct WSL uses a dedicated key (`~/.ssh/id_ed25519_conference1`);
`IdentitiesOnly yes` keeps other agent keys from being offered first.

First contact: the bootstrap and closeout gates run SSH with
`BatchMode=yes`, which cannot answer an interactive host-key prompt. Accept
each host key once before first use — either run a manual
`ssh <alias> true` and answer `yes`, or set
`StrictHostKeyChecking accept-new` in the Host block if trust-on-first-use
is acceptable for your threat model.

## Supported surfaces

| Surface | Transport | Clipboard | Notes |
|---------|-----------|-----------|-------|
| Operator macOS + Ghostty + local tmux | local | Required | Ghostty needs `--clipboard-write=allow` |
| skillbox-portfolio-devbox (d3) | SSH or mosh | Required | Default d3 portfolio devbox |
| Remote tmux inside d3 | nested | Required | Managed tmux fragment + `clipcopy` |
| Sweet Potato (`aiops@sweet-potato-prod`) | SSH | Required | |
| Jeremy (`skillbox@skillbox-jeremy-3`) | SSH | Required | |
| Conference1 direct WSL (`worker@conference1-wsl`) | SSH or mosh | Required | **Preferred** Conference path |
| Conference1 Windows wrapper (`conference1-ssh`) | WSL via Windows | Known-bad | OSC52-hostile; recovery/auth fallback only |
| Generic `user@host` | SSH | Best-effort | Profile `generic` or raw target arg |

### Operator-platform matrix

| Operator client | Capture | Gesture/install | Support level | Proof boundary |
|---|---|---|---|---|
| macOS + Ghostty + local tmux | AppKit pasteboard with monotonic change count | Managed `Cmd+V`/`Ctrl+V`; `clipboard-bootstrap` install/uninstall | Core | Real Mac + `devbox-1` image proof recorded; remaining transport rows still gate rollout |
| Linux Wayland | `wl-paste`, typed MIME capture, content-digest generation | Same Ghostty/tmux fragment; `clipboard-bootstrap --profile local` and `uninstall` | Experimental | Offline command fixtures only; no real image or native-text regression proof yet |
| Linux X11 | `xclip`, typed target capture, content-digest generation | Same as Wayland | Experimental | Offline command fixtures only |
| Native Windows | STA PowerShell image/file/text capture contract | No safe terminal-focus installer is shipped | Substrate only, unsupported for rollout | Offline payload fixture only; no real image or paste proof |
| WSL via Windows terminal | Not inferred from native Windows state | Existing Conference fallback remains explicit and OSC52-hostile | Unsupported | Never counted as a passing Linux or Windows client |

Linux and Windows do not expose AppKit's monotonic pasteboard change count. The
experimental capture substrate derives a generation from the typed content and
re-reads it inside the same explicit gesture immediately before injection. It
does not poll or retain history. Neither platform is promoted to supported
until its separate installer/uninstaller, native-text regression, focus-race,
and real-image proof all pass. The macOS default is unchanged.

Remote bootstrap writes the owned helper and configuration files but never
sources configuration into an already-running tmux server. Existing remote
sessions stay live and untouched; new tmux servers pick up the tracked source
line normally.

## OSC52 and tmux behavior

`clipcopy` reads stdin, then:

1. On local macOS without SSH env: `/usr/bin/pbcopy`.
2. Inside tmux: `tmux load-buffer`, then writes OSC52 (`\033]52;c;<b64>\a`) to each
   attached client TTY from `tmux list-clients -F '#{client_name}'`. This updates the
   operator clipboard when `tmux load-buffer -w` alone would not.
3. Falls back to `tmux load-buffer -w`, then direct OSC52 to `/dev/tty` or stdout.

The managed tmux fragment (`scripts/clipboard/tmux.conf`) sets `set-clipboard on`,
terminal clipboard features for `xterm-ghostty` and nested `tmux*`, and binds
copy-mode to `$HOME/.local/bin/clipcopy`.

Remote hosts need `xterm-ghostty` terminfo. Bootstrap installs from the bundled
`scripts/clipboard/xterm-ghostty.tic` when the terminfo is absent; it falls back
to `infocmp -x xterm-ghostty | tic -x -` when the host already has a source entry.

## Conference1 routing

Probe order (encoded in `scripts/clipboard/hosts.json`):

1. `ssh conference1-wsl true` — direct WSL reachable → use `worker@conference1-wsl`.
2. `ssh conference1-wsl 'command -v mosh-server'` — if mosh-server exists, prefer mosh.
3. Only when direct WSL is unreachable: fall back to `conference1-ssh` (Windows wrapper).

**Do not** use `conference1-ssh` for clipboard-sensitive work when direct WSL is up.
The wrapper path is documented and tested as OSC52-hostile.

## Explicit recovery (`clipimg-put`)

Darwin-only. Extracts PNG/TIFF from macOS clipboard, uploads to
`~/clipboard-images/clipboard-<timestamp>.png` on the remote, puts the **remote file
path** on the Mac clipboard. Use it only when `clipboard-paste doctor` says the
one-key route is unavailable.

This is the explicit recovery path, not the target daily UX. The normative
one-gesture behavior, latency budget, route rules, and threat controls live in
[`docs/seamless-remote-paste.md`](seamless-remote-paste.md).

Conference target `c` resolves to direct WSL (`worker@conference1-wsl`), not the
Windows wrapper.

## Security boundaries

- Reads media bytes only after an explicit paste gesture; it never polls the clipboard.
- Installs only under the target user's home: `~/.local/bin`, `~/.config/skillbox/`,
  scoped Ghostty/tmux includes, and a private lifecycle manifest.
- Remote artifacts are content-addressed, mode `0600`, bounded to 20 MiB, and
  pruned by a 7-day/200-MiB policy. Transient Mac snapshots are destroyed after
  remote success or failure. A newly-created remote artifact is deleted when a
  post-transfer focus, route, clipboard, or pane-injection check cancels paste.
- No secrets, no system-wide terminfo, no public `0.0.0.0` binding changes.
- Remote bootstrap uses SSH; dry-run/plan modes never write.

## Adoption checklist

Canonical new-host flow: see "New-host clipboard adoption" in
`docs/operations.md`. Remote profiles are plan-only by default; remote writes
happen only with `--apply-remote`.

```bash
# From Skillbox repo root — local operator Mac (applies locally)
scripts/clipboard-bootstrap --profile local

# Remote host (d3): prints the plan, performs no remote writes
scripts/clipboard-bootstrap --profile d3

# One command applies the reversible local bundle and remote receiver
scripts/clipboard-bootstrap --profile d3 --apply-remote

# Generic target (implies --profile generic)
scripts/clipboard-bootstrap --target skillbox@my-host --dry-run

# Closeout / regression (CI smoke)
scripts/clipboard-closeout.sh
```

Status and lifecycle:

```bash
clipboard-paste status --profile d3
clipboard-paste doctor --profile d3
clipboard-paste doctor --profile d3 --probe-target  # explicit `ssh ... true`
clipboard-paste explain --profile d3 --json
clipboard-metrics                 # local receipts only; no host contact

scripts/clipboard-bootstrap rollback
scripts/clipboard-bootstrap uninstall
# Restore remote + local baselines explicitly:
scripts/clipboard-bootstrap uninstall --profile d3 --apply-remote
```

Inside tmux, the status command resolves the current pane, attached client,
route path, and generation automatically. `ready` therefore means an exact
route was revalidated; an installed bundle with no exact current route reports
`ambiguous` and recommends relaunching through `d2` or `d3`.
Target reachability is never implicit: `--probe-target` runs exactly one
non-interactive `ssh ... true` check, never lists or attaches sessions, and
reports only `attempted`, `reachable`, and a redacted stable error class under
`target_probe`. Public status/doctor JSON also replaces the SSH target and
remote-session label with state labels, shortens the latest receipt to its
filename, and rewrites absolute operator-HOME prefixes as `~`. A malformed or
unsafe lifecycle manifest becomes a failing `lifecycle.manifest` check instead
of a traceback.
The `agent.adapter` object is also evidence-backed: Codex path attachment is
reported ready only for the proven minimum `0.144.4` or newer. Older or unknown
versions degrade to a visible text-reference strategy with an update repair.

`clipboard-metrics` requires 20 successful samples before it calls a latency
distribution rollout-grade. A smaller set remains diagnostic evidence only.

Routine tests never inspect the live clipboard. The one macOS capture observer
is opt-in and should be run only with a deliberate proof image already copied:

```bash
SKILLBOX_LIVE_CLIPBOARD_TEST=1 \
  pytest -q tests/test_clipboard_snapshot.py::ClipboardSnapshotTests::test_live_capture_does_not_change_pasteboard_generation
```

Install baselines and one-step rollback snapshots live under
`~/.local/state/skillbox/clipboard-bootstrap/`. Uninstall restores overwritten
helpers and removes only Skillbox-owned Ghostty/tmux blocks while preserving
later user edits.

## Closeout gates and proof commands

`scripts/clipboard-closeout.sh` is the closeout/regression gate. It has two
documented modes; the JSON report always distinguishes mocked/unit proof from
live terminal proof.

### CI / source smoke (runs on any Linux checkout)

```bash
scripts/clipboard-closeout.sh
# equivalent unit-only invocation:
python3 -m unittest tests.test_clipboard_bootstrap tests.test_clipboard_closeout -v
```

Gates: unit/fixture tests (installer idempotency, route selection, target
mapping, tilde normalization, tmux fragment content, clipcopy client-TTY
behavior, verdict policy), static checks (`bash -n` on every clipboard shell
script, `shellcheck` when available, `git diff --check`), and the bootstrap
`--help` / `--dry-run d3` launch proof. Live terminal paths are recorded as
**SKIP with a named reason per path/host**; the report states explicitly that
skips were allowed only because the run is non-live. Overall PASS is expected.

### Operator / live rollout proof

```bash
scripts/clipboard-closeout.sh --live
```

Adds live terminal paths, each recorded with target host, transport, raw log
path, and PASS/FAIL/SKIP:

- `current_host_migration` — the runner itself uses the managed bundle
  (clipcopy sha, managed tmux fragment, source line, terminfo)
- `local_tmux` — temp-socket tmux + bundle fragment; `clipcopy` must land in
  the tmux buffer with `set-clipboard on`
- `direct_ghostty_osc52` — Ghostty OSC52 write (operator Mac only)
- `mosh_transport` — mosh OSC52 (interactive operator terminal only)
- `ssh_osc52_{d3,sweet,jeremy}` / `conference_direct_wsl` — real SSH with a
  forced tty; the emitted OSC52 payload is captured and base64-decoded back
  to the marker
- `host_state_*` — remote helper executability + sha, tmux fragment sourced,
  `xterm-ghostty` terminfo
- `remote_tmux_*` — temp-socket remote tmux buffer proof + `set-clipboard on`
- `nested_tmux` — local tmux -> SSH -> remote tmux; the OSC52 written to the
  remote tmux client TTY must arrive in the **local** outer tmux buffer
- `image_transfer_{d,s,j,c}` — synthetic PNG upload with remote
  existence/sha check and remote temp-file removal (same target mapping as
  `clipimg-put`; the Mac clipboard read itself is Darwin-only)
- `local_clipboard_restore` and `cleanup_temp_artifacts` — the gate restores
  the local clipboard (Darwin) or proves it never touched it (Linux), and
  kills/removes every temp tmux session and socket file it created, locally
  and remotely.

**Fail-closed policy:** in `--live` mode every live path is core. A skipped
core path (e.g. the d3 path, the current-host migration proof, Ghostty/mosh
when unreachable) is a blocking failure — a live run can never report overall
PASS while a core path was skipped. `CLIPBOARD_LIVE_TARGETS` can subset the
hosts for debugging, but excluded hosts are recorded as SKIP and still force
overall FAIL. A live run from a Linux box therefore reports FAIL by design
(Ghostty/mosh/Mac paths cannot run there); the full-PASS rollout proof must
run from the operator Mac.

Artifacts: each run writes a durable directory
`~/.local/state/skillbox/clipboard-closeout/<stamp>-<mode>/` (override with
`CLIPBOARD_CLOSEOUT_DIR` or `--artifact-dir`) containing
`clipboard-closeout.json` (per-gate status, exit code, target host,
transport, reason, log path, excerpt, overall verdict, blocking list) plus
one raw log per gate; `latest` symlinks the newest run.

Legacy direct Ghostty-only proof (operator Mac):

```bash
scripts/clipboard-proof.sh --live   # SKIP on non-Darwin or without Ghostty
```

See `docs/troubleshooting.md` for failure modes.

## Field report: what still hurt?

After a real attempt, preserve the operator's own words and attach the redacted
truth surfaces. Do not include clipboard contents, remote paths, hostnames, pane
text, or session listings.

```text
Gesture and surface: [Cmd+V or Ctrl+V] in [d2 or d3], [SSH or mosh]
Expected:
Observed:
What was surprising or required a second action:
Recovery used, if any:
clipboard-paste status --profile d3 --json: [attach redacted output]
clipboard-paste doctor --profile d3 --json: [attach redacted output]
Receipt/error class and elapsed_ms: [no paths or clipboard data]
Would this feel local if the issue disappeared? [yes/no, why]
```
