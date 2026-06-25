# Tailnet-Only Box Lifecycle

Managed Skillboxes default to `tailnet_only` network posture. Public SSH is a
bootstrap aperture that closes after Tailscale enrollment. This document covers
the lifecycle, recovery, and posture verification commands.

## Lifecycle Stages

```
create → bootstrap → ssh-ready → enroll → lockdown → deploy → acceptance → ready
```

| Stage | Network | What happens |
|-------|---------|--------------|
| create | Public SSH open, Tailscale UDP open (cloud firewall) | Droplet created, bootstrap firewall applied |
| bootstrap | Same | Host scripts installed over public SSH |
| ssh-ready | Same | Public SSH verified reachable |
| enroll | Same | Tailscale joined, `TAILNET_ONLY_SSH=true` locks host UFW |
| lockdown | Tailscale UDP only (cloud firewall updated) | Cloud firewall drops public SSH; host UFW already locked |
| deploy | Tailnet only | Release installed over Tailscale SSH |
| ready | Tailnet only | Box operational; public SSH = policy drift |

After lockdown, the only inbound path is Tailscale (UDP 41641). Public SSH is
unreachable. All subsequent `box ssh`, `box status`, and deploy commands
connect via Tailscale IP or MagicDNS hostname.

## Network Posture Values

| Posture | Meaning |
|---------|---------|
| `tailnet_only` | Default for managed boxes. No public SSH after lockdown. Cloud firewall required. |
| `public` | Public SSH allowed. No cloud firewall enforced. |
| `unmanaged` | External/registered boxes. No policy enforcement. |

## Exposure Classifications

Services bind to one of four exposure patterns:

| Classification | Example bind | Allowed under `tailnet_only` |
|---------------|-------------|------------------------------|
| `loopback-only` | `127.0.0.1:8080` | Yes |
| `tailnet-direct` | `100.x.y.z:3210` | Yes |
| `ingress-routed` | via Tailscale Funnel/proxy | Yes |
| `wildcard-direct` | `0.0.0.0:8080` | **No** — violation |

Pulse also runs a port sentinel. In `observe` mode it reports unmanaged
listeners and wildcard/dev-server signatures in `pulse.state.json`; in
`enforce` mode it may terminate dev-server signatures after the configured
grace window. Unknown non-dev listeners remain report-only.

## Commands

### Verify posture from operator machine

```bash
# Posture proof artifact (JSON by default)
python3 scripts/box.py posture-proof <box-id>
python3 scripts/box.py posture-proof <box-id> --format text

# Box health includes posture and violations
python3 scripts/box.py status <box-id> --format json
```

### Posture proof output shape

```json
{
  "box_id": "my-box",
  "posture": "tailnet_only",
  "cloud_firewall_rules": { "...": "..." },
  "public_ssh_probe": { "ok": false, "target": "1.2.3.4" },
  "tailnet_probe": { "ok": true, "target": "100.100.1.1" },
  "timestamp": "2026-06-10T12:00:00+00:00",
  "violations": []
}
```

A clean proof has `public_ssh_probe.ok == false` (unreachable) and
`tailnet_probe.ok == true` (reachable) with zero violations.

### Runtime exposure lint

```bash
# Inside the box, dev-sanity checks service binds against posture
SKILLBOX_NETWORK_POSTURE=tailnet_only make dev-sanity
```

## Recovery

### Tailscale down, need public SSH

If Tailscale is unreachable and you need to recover:

1. Temporarily open SSH in the cloud firewall via DO console or `doctl`
2. SSH in via public IP — `box ssh` will warn "recovery mode only"
3. Fix Tailscale
4. Re-lock the cloud firewall:
   ```bash
   python3 scripts/box.py posture-proof <box-id>
   ```
5. Verify proof shows `public_ssh_probe.ok == false`

### Stale SSH target cache

`resolve_box_ssh_target` skips stale public IP caches for `tailnet_only` boxes.
If a box was previously accessed via public IP, the next connection attempt
will try Tailscale targets first. Public IP is tried last as a recovery
fallback and is not cached as `last_ssh_target`.

## Teardown

`box down` deletes the cloud firewall before destroying the droplet, and only
marks the box `destroyed` after the droplet's absence is **API-confirmed**:

```
drain → remove from tailnet → delete firewall → destroy droplet
      → confirm absent (read-after-delete) → cleanup volume → destroyed
```

### Teardown truth invariant

A fleet inventory that says `destroyed` while a droplet still bills is the most
expensive lie. `box down` therefore never trusts the `doctl ... droplet delete`
exit code alone: after the delete call it issues a bounded read-after-delete
confirmation (`doctl compute droplet get <id> --output json`, the same JSON
parse used elsewhere) and only writes `destroyed` once the droplet is observed
absent (a 404 / empty result). DigitalOcean's delete is eventually consistent,
so the confirm performs **one bounded retry with linear backoff** and then lands
in a truthful pending state — it never spins or hangs.

Tailscale removal is best-effort: a failed `tailscale logout` is reported as a
`remove` step `warn` but never blocks droplet destruction.

### Teardown states

| State | Meaning | Billing risk | Reachable next state(s) | Retry |
|-------|---------|--------------|-------------------------|-------|
| `draining` | Services stopped; tailnet/firewall/droplet teardown in progress | Possible (droplet may still exist) | `destroy-pending`, `volume-cleanup-failed`, `destroyed` | `box down <id>` |
| `destroy-pending` | Droplet delete was requested but the droplet is **still API-listed** (read-after-delete not yet confirmed) | **Yes** — droplet may still bill; inventory deliberately does NOT say `destroyed` | `destroy-pending`, `volume-cleanup-failed`, `destroyed` | `box down <id>` (re-confirms absence; never re-deletes) |
| `volume-cleanup-failed` | Droplet **confirmed gone**, but the attached volume could not be detached/deleted | No — droplet is gone | `volume-cleanup-failed`, `destroyed` | `box down <id>` (retries volume cleanup only) |
| `destroyed` | Droplet confirmed absent and volume cleanup complete (or no volume) | None | terminal | n/a |

Both `destroy-pending` and `volume-cleanup-failed` are surfaced in
`box status <id>` and `box list` (a `teardown_pending` block carrying the exact
`box down <id>` retry command and a `billing_risk` flag), not just in the
output of the `box down` command that produced them. Re-running `box down` from
either state is idempotent and converges to `destroyed` once the underlying
infrastructure cooperates.

> Registered/external boxes (`management_mode: external`) have no managed
> droplet to confirm and are out of scope for teardown — use `box unregister`.

## Cautions

- Do not change posture on a live box without verifying the cloud firewall
  matches. Use `posture-proof` to check.
- `wildcard-direct` binds (`0.0.0.0`) are violations under `tailnet_only`.
  Fix service configs to bind to Tailnet IP or loopback.
- Keep `SKILLBOX_PORT_SENTINEL=observe` until the pulse telemetry is clean;
  `enforce` is intended for dev-server signatures, not arbitrary operator
  sockets.
- The bootstrap aperture (public SSH) exists only during `create` through
  `enroll`. After lockdown, there is no public SSH path.
