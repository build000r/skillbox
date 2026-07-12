# Conference1 tailnet Serve and heavy-build lane

Conference1 is a Windows box (with WSL Ubuntu) on the tailnet used as a
heavy-build and dev-surface target. Its metadata lives in the existing host
registry `scripts/clipboard/hosts.json` under the `conference1_tailnet` key and
is surfaced through `sbp conference1` (aliases: `conf1`, `tailnet`), backed by
`scripts/lib/conference1_tailnet.py`.

## Host facts

| Fact | Value |
| --- | --- |
| MagicDNS | `conference1.tail4c481e.ts.net` |
| Tailscale IP (Windows) | `100.123.217.11` |
| Windows SSH (automation) | `ssh conference1-ssh` (`worker@100.123.217.11`) |
| WSL Ubuntu SSH (builds) | `ssh worker@conference1-wsl` (`100.96.206.87`) |
| Serve helper (Windows) | `C:\Users\worker\bin\tailnet-serve.ps1 list|expose|remove` |
| Served ports | 3000, 3001, 3170, 3210, 8050 → `127.0.0.1` backends |

Both SSH host aliases and the dedicated key (`~/.ssh/id_ed25519_conference1`)
are defined in `~/.ssh/config` on this box.

## Commands

```sh
sbp conference1              # offline: endpoints, helper commands, lane, posture
sbp conference1 status       # live, read-only: Serve list + portproxy list over SSH
sbp conference1 helper       # exact tailnet-serve.ps1 / netsh commands, nothing executed
sbp conference1 status --json
```

`status` output labels the two surfaces explicitly:

- **MagicDNS Serve URLs (primary)** — `http://conference1.tail4c481e.ts.net:<port>`,
  managed by Tailscale Serve via `tailnet-serve.ps1`. Use these.
- **Raw Tailscale-IP portproxy (fallback only)** — `http://100.123.217.11:<port>`
  via `netsh interface portproxy`; only when MagicDNS Serve is unavailable.

Mutations always require an explicit `--yes` (default prints the exact command
as a dry-run):

```sh
sbp conference1 expose <port> --yes
sbp conference1 remove <port> --yes
```

App startup stays in each repo's own scripts on Conference1; this surface only
manages/inspects Tailscale Serve and the portproxy fallback.

If Windows-side SSH is unreachable, `status` exits nonzero with error code
`CONFERENCE1_SSH_UNREACHABLE` and prints the manual check path (tailscale
status, a `BatchMode` ssh probe of `conference1-ssh`, the `~/.ssh/config`
entry, and the WSL fallback probe).

## Swimmers remote Rust validation lane

- `SWIMMERS_REMOTE_RUST_HOST=conference1-ssh` (or `worker@100.123.217.11`)
- Builds run in WSL Ubuntu, reachable directly as `worker@conference1-wsl`
- Persistent cargo cache: `/var/tmp/swimmers-remote-rust-cache` (owned by
  `worker`, verified present)

## Security rule (tailnet-only posture)

- Reach Conference1 services through Tailscale Serve / MagicDNS URLs on the
  tailnet only.
- No `0.0.0.0` public binds: apps bind `127.0.0.1`; Serve (or, as fallback,
  the tailnet-IP portproxy) proxies to them. Note the portproxy listens on the
  Tailscale IP `100.123.217.11`, not `0.0.0.0`.
- **No Tailscale Funnel** unless the operator explicitly requests it. The
  `sbp conference1` surface refuses any argument containing "funnel".

Posture check: `sbp conference1 status` — every Serve mapping should read
"(tailnet only)" and every portproxy listen address should be `100.123.217.11`.
Manual path if SSH is down: from any tailnet machine confirm the URLs above
resolve only over the tailnet, and on Conference1 run
`tailscale serve status` and `netsh interface portproxy show v4tov4`.

## Tests

`tests/test_conference1_tailnet.py` covers metadata rendering (including a
secret-leak guard and redaction of live output), MagicDNS-vs-portproxy
labeling, the read-only SSH argv shapes, the unreachable error path, the
`--yes` mutation gate, and the Funnel refusal.
