# Clipboard bootstrap

Skillbox owns OSC52 clipboard integration for operator Mac + Ghostty, local tmux,
SSH/mosh remotes, nested tmux, and Conference1 WSL. Source bundle:
`scripts/clipboard/`. Bootstrap entry: `scripts/clipboard-bootstrap`.

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

## Image transfer (`clipimg-put`)

Darwin-only. Extracts PNG/TIFF from macOS clipboard, uploads to
`~/clipboard-images/clipboard-<timestamp>.png` on the remote, puts the **remote file
path** on the Mac clipboard. True binary paste through the terminal is not supported.

Conference target `c` resolves to direct WSL (`worker@conference1-wsl`), not the
Windows wrapper.

## Security boundaries

- Installs only under the target user's home: `~/.local/bin`, `~/.config/skillbox/`,
  `~/.tmux.conf` source line (append-only, idempotent).
- No secrets, no system-wide terminfo, no public `0.0.0.0` binding changes.
- Remote bootstrap uses SSH; dry-run/plan modes never write.

## Adoption checklist

```bash
# From Skillbox repo root — local operator Mac
scripts/clipboard-bootstrap --profile local

# Remote host (d3 default)
scripts/clipboard-bootstrap --profile d3

# Plan without writes
scripts/clipboard-bootstrap --profile d3 --dry-run

# Generic target (implies --profile generic)
scripts/clipboard-bootstrap --target skillbox@my-host --dry-run

# Closeout / regression
scripts/clipboard-closeout.sh
```

## Proof commands

Unit/fixture proof (always runnable):

```bash
python3 -m unittest tests.test_clipboard_bootstrap -v
scripts/clipboard-closeout.sh
```

Live Ghostty/Mac proof (operator environment):

```bash
scripts/clipboard-proof.sh --live   # SKIP on non-Darwin or without Ghostty
```

See `docs/troubleshooting.md` for failure modes.