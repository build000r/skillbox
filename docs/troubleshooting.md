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
