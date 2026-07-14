# Troubleshooting

This page contains content moved from `README.md` during the README front-door split.

## Troubleshooting

### `make doctor` fails on Compose validation

Check that Docker is installed and `docker compose config --format json` works on the host.

### `make dev-sanity` warns about missing log directories or managed skill installs

That is expected on a fresh clone. The core runtime graph declares
`.skillbox-state/logs/runtime`, `.skillbox-state/logs/repos`, and the managed skill install roots plus
lockfile are also created on demand.

Run:

```bash
make runtime-sync
make dev-sanity
```

### `make up-surfaces` starts but ports are unreachable

The API and web surfaces bind to `127.0.0.1` by design. Use local forwarding or a host shell, not a public interface.

### Skill sync fails

Run:

```bash
python3 .env-manager/manage.py sync --dry-run --format json
python3 .env-manager/manage.py doctor --format json
```

Check for `SKILL_REPO_UNREACHABLE` (auth/network) or `SKILL_NOT_FOUND_IN_REPO` (bad pick list).
First-box acceptance also runs a `skill-availability` preflight after sync; if
it fails, declared skills are not installed cleanly into both managed
`~/.claude/skills` and `~/.codex/skills` roots.

### SSH login says identity is unknown

The shared SSH login hook no longer prints a warning for every non-interactive
SSH command. For an interactive diagnostic, set `SKILLBOX_LOGIN_WARN_IDENTITY=1`
and reconnect; then check `SSH_CLIENT`, `tailscale whois <client-ip>`, and
Tailnet reachability.

### SSH works to the host but not the box

That is expected. SSH targets the host, not the workspace container. Use `make shell` after connecting.

### `make runtime-status` shows repos as missing

The internal runtime manager evaluates host paths that correspond to the
container's `/workspace/...` and `/monoserver/...` trees.

Run:

```bash
make runtime-sync
make runtime-status
```

If a repo is still missing after sync, the runtime entry is probably configured
with `sync.mode: external` and expects a bind mount from `/monoserver` or a
manual clone under `/workspace/repos`.

If the missing repo belongs to a client overlay, check it explicitly:

```bash
make runtime-status CLIENT=personal
make runtime-status CLIENT=vibe-coding-client
```

### Default skills look stale

Re-run:

```bash
make runtime-sync
make doctor
```

### Pulse daemon won't start

Check if it's already running:

```bash
make pulse-status
```

If the PID file is stale (process died without cleanup), remove it from the
state root:

```bash
rm .skillbox-state/logs/runtime/pulse.pid
make pulse-start
```

### Operator tools are blocked by the guard hook

The destructive-op guard requires:
1. All git repos committed and pushed
2. A `dry_run=true` call before the real operation

Run `/commit`, push, then re-run with `dry_run: true` first.

### Clipboard copy does not reach the Mac

Check in order:

1. Ghostty launched with `--clipboard-write=allow` (live terminals only; captured PTYs may not pass OSC52).
2. `~/.local/bin/clipcopy` exists and is executable on the host where copy runs.
3. `~/.config/skillbox/clipboard.tmux.conf` is sourced from `~/.tmux.conf`.
4. Remote host has `xterm-ghostty` terminfo: `infocmp -x xterm-ghostty >/dev/null`.
5. Inside tmux, `clipcopy` writes OSC52 to attached client TTYs — not only `tmux load-buffer -w`.

Re-bootstrap:

```bash
scripts/clipboard-bootstrap --profile local
scripts/clipboard-bootstrap --profile d3 --apply-remote
scripts/clipboard-closeout.sh
```

### Cmd+V or Ctrl+V does not create an image attachment

Run the truth surface first; it never prints clipboard bytes:

```bash
clipboard-paste status --profile d3
clipboard-paste doctor --profile d3
clipboard-paste explain --profile d3 --json
```

Then check that `ghostty +list-keybinds` shows the Skillbox `super+v` and
`ctrl+v` private sequences, tmux shows `User198`/`User199`, and the focused
pane was launched through tracked `d2`/`d3`. A stale or unknown route will keep
native text paste but refuse image upload. Press the chord again to retry after
repair; cancel an in-flight pane explicitly with
`clipboard-smart-paste --cancel --pane %N --client /dev/ttysN`.

### Conference1 clipboard fails over SSH

Use direct WSL (`worker@conference1-wsl`), not the `conference1-ssh` Windows wrapper.
The wrapper path is documented as OSC52-hostile. Probe:

```bash
ssh conference1-wsl true
ssh conference1-wsl 'command -v mosh-server'
```

### `clipimg-put` fails or pastes wrong content

- This is the explicit recovery command, not the daily path.
- It must run on macOS with an image on the clipboard (PNG or TIFF).
- Uploads to `~/clipboard-images/` on the remote and puts the **remote path** on the Mac clipboard.
- Paste the returned path into chat, or repair the one-key path with
  `clipboard-paste doctor`.

## Limitations

- This is not a hosted control plane or a multi-user workspace platform.
- Skill distribution is still private and explicit: local publisher, preview,
  sync, and rollback primitives are implemented, but a hosted distributor
  service, standalone laptop CLI, background update checks, and short-lived
  token exchange are still future work.
- The API and web surfaces are inspection stubs, not a full UI.
- The internal runtime manager now does dependency-aware task and service orchestration plus managed env hydration, but it still does not try to replace app-specific deployment systems or CI.
- Secrets management and app-specific bootstrap details beyond what you declare in your overlays and blueprints are still your responsibility.
- The pulse daemon is single-process; it does not survive container restarts unless declared as a managed service in `runtime.yaml`.
- Fleet management requires DigitalOcean and Tailscale credentials. Other cloud providers are not supported.
- There is no license file in this repo yet. Add one before publishing it as open source.
