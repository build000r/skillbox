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

`network.containment` fails closed when `lsof` is unavailable or errors; it
never treats a missing observation as proof that no listener exists. Likewise,
`files.private_modes` rejects group/world-readable state directories or files,
symlinks, and non-regular entries without following them.

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
```

`d3 c` / `d2 c` default to SSH on that path. Do not treat `mosh-server` presence
as proof the session should use mosh: `conference1-wsl` goes through a Windows
SSH `ProxyCommand`, which cannot carry mosh UDP. Manual mosh is only for an
operator who has already proven a non-proxy route
(`DEVL_CONFERENCE_TRANSPORT=mosh d3 c`).

### `clipimg-put` fails or pastes wrong content

- This is the explicit recovery command, not the daily path.
- It must run on macOS with an image on the clipboard (PNG or TIFF).
- Uploads to `~/clipboard-images/` on the remote and puts the **remote path** on the Mac clipboard.
- Paste the returned path into chat, or repair the one-key path with
  `clipboard-paste doctor`.

## DCG recovery

Full setup, verify semantics, upgrade and uninstall live in
[operations.md](operations.md#dcg-destructive-command-guard). This section is
the "it is not healthy, now what" path.

Start with the read-only verdict — it never changes anything:

```bash
python3 .env-manager/manage.py dcg-reconcile --action verify --format json
python3 .env-manager/manage.py doctor --format json      # `dcg` check, read `dcg_status`
```

| Symptom | Cause | Fix |
| --- | --- | --- |
| exit 3, `codex_trust: absent` (**CODEX_HOOK_TRUST_REQUIRED**) | Codex has never trusted the hook | Start Codex in this home and trust the `dcg` hook in its review modal |
| exit 3, `codex_trust: stale` | the hook changed since it was trusted | Re-trust it in the same modal; the persisted hash no longer matches |
| `BINARY_VERSION_MISMATCH` | installed `dcg` differs from the pin | Re-run `make dcg-reconcile`; never "fix" it by installing latest |
| `POLICY_FAIL_OPEN` | policy lost `fail_closed = true` | Re-run `make dcg-reconcile` to re-render the policy |
| `HOOK_DUPLICATE` | a second DCG entry was added by hand | Re-run `make dcg-reconcile`; it de-duplicates DCG-owned entries |
| Hooks look right but nothing is guarded | the command never reached a hook | Expected for a **direct shell** or `unified_exec` — see the coverage table |

Rules that matter more than any individual fix:

- **Never hand-edit `~/.claude/settings.json`, `~/.codex/hooks.json`,
  `~/.grok/hooks/dcg.json` or the rendered policy to make a check pass.** A
  hand-edited hook drifts on the next converge, and the edit is invisible to the
  ledger, so `rollback` cannot undo it. Re-run `make dcg-reconcile` instead.
- **Never pass `--dangerously-bypass-hook-trust`.** It yields a host that
  advertises a hook while nothing enforces it.
- **Never install an unpinned or `latest` build to clear a version mismatch.**
  The pin is the contract; convergence verifies against it.

If convergence made something worse, `runtime_manager.dcg_reconcile.rollback()`
restores the bytes from the last mutating run. It depends on the ledger and
backup set, so a prior `--purge` removes that option.

To verify a fix end to end without touching your real home, use a disposable
one — this is the flow the operator docs are tested against:

```bash
python3 - <<'PY'
import sys, tempfile
from pathlib import Path
sys.path.insert(0, ".env-manager")
from runtime_manager import dcg_reconcile as R
home = Path(tempfile.mkdtemp()) / "home"; home.mkdir(parents=True)
print(R.apply(home)["status"])      # needs-operator-action (Codex trust is absent)
print(R.verify(home)["status"])     # still needs-operator-action
PY
```

It reports `needs-operator-action` until the Codex hook is trusted, and
`healthy` afterwards. That transition is the point: trust is a human step, and
nothing in Skillbox forges it.

## Limitations

- **DCG does not guard every command.** It is a PreToolUse hook, so a command
  typed into a **direct shell** never reaches it, and Codex `unified_exec` keeps
  a session open where DCG sees the invocation but not each subsequent command.
  A `healthy` DCG check means the hook contract is converged — not that nothing
  runs unguarded. `manage.py doctor` prints these limitations on every run,
  healthy included, for exactly that reason.
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
