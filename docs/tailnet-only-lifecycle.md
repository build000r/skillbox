# Tailnet-Only Box Lifecycle

Managed boxes default to `tailnet_only`: public SSH is a temporary bootstrap
aperture through `enroll`; after Tailscale enrollment succeeds, `box.py` locks
host SSH to Tailnet access and updates the DigitalOcean firewall so inbound
public SSH is closed. `posture-proof` verifies the box-level result with
`public_ssh_probe`, `tailnet_probe`, `cloud_firewall_rules`, and `violations`;
service bind exposure is verified by the runtime exposure lint. This document
covers the lifecycle, break-glass recovery, and posture verification commands.

> Design rationale, 2026-04-08: an operator lockout incident showed that
> closing public SSH before Tailscale enrollment is proven can strand the box.
> The current lifecycle keeps public SSH only for create/bootstrap/ssh-ready and
> enrollment, then closes it after a Tailscale address exists. Recovery relies
> on the DigitalOcean droplet console or an explicitly temporary firewall
> aperture, not on leaving public SSH open by default.

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

After lockdown, the only intended inbound path is Tailscale. The DigitalOcean
firewall keeps inbound UDP 41641 for Tailscale and drops public TCP 22; host
UFW accepts SSH from the Tailnet CIDR / `tailscale0`. Public SSH reachability
after this point is drift or break-glass recovery state. All subsequent
`box ssh`, `box status`, and deploy commands prefer Tailscale IP or MagicDNS
hostname.

## Network Posture Values

| Posture | Meaning |
|---------|---------|
| `tailnet_only` | Default for managed boxes. Public SSH is temporary through `enroll`; after lockdown, public SSH is closed and a cloud firewall is required. |
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

Runtime sync also writes generated repo-local port contracts for covered HTTP
services. Each covered repo gets `.skillbox-port.env` with `PORT`, `HOST`, and
`SKILLBOX_SERVICE_ID` from the port registry. Client repos should gitignore
that file and load it before dev startup; Vite apps should set
`server.strictPort: true` with `port: Number(process.env.PORT)` so a busy
declared port fails loudly instead of auto-incrementing.

The sentinel default stays `observe` until the port-guard telemetry has at
least 14 consecutive days of clean evidence: zero wildcard criticals and no
operator-confirmed false-positive reports. The proof path is
`scripts/port-guard-proof.sh`, which writes a dated report with the five
criterion checks, current port registry, doctor output, and pulse counters.
Only after that clean window should the default flip to `enforce`, and that
flip should be recorded as a dated config change.

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

### Guarantee to proof mapping

Every posture claim in this document maps to an implemented check:

| Documented guarantee | Implemented check | Proof field / command |
|---|---|---|
| Managed boxes default to `tailnet_only` unless explicitly configured otherwise. | `resolve_network_posture()` returns `tailnet_only` for managed inventory entries with no explicit posture. | `posture` in `python3 scripts/box.py posture-proof <box-id>` |
| Public SSH is closed after lockdown. | `posture-proof` attempts SSH to `droplet_ip`; `evaluate_posture_violations()` emits `public_ssh_reachable` if it succeeds under `tailnet_only`. | `public_ssh_probe.ok == false` and no `public_ssh_reachable` in `violations` |
| Tailnet reachability exists after lockdown. | `posture-proof` runs `tailscale ping --timeout=2s --c=1` against `tailscale_ip` or `tailscale_hostname`. | `tailnet_probe.ok == true` |
| A managed `tailnet_only` box has a cloud firewall associated. | `evaluate_posture_violations()` emits `cloud_firewall_missing` when `cloud_firewall_id` is absent; `posture-proof` fetches rules when the ID is present. | `cloud_firewall_rules != null` and no `cloud_firewall_missing` in `violations` |
| The cloud firewall should keep Tailscale UDP and drop public SSH after lockdown. | `box.py` creates bootstrap rules with public TCP 22, then `do_update_firewall_lockdown()` replaces inbound rules with UDP 41641 only. | `cloud_firewall_rules` is included for audit; current `posture-proof` does not parse those rules into a separate pass/fail result. The public-SSH probe is the active behavior check. |
| Wildcard direct service binds violate `tailnet_only`. | Runtime exposure lint classifies service endpoints and fails `wildcard-direct` under `SKILLBOX_NETWORK_POSTURE=tailnet_only`. | `SKILLBOX_NETWORK_POSTURE=tailnet_only make dev-sanity` (`service-exposure-violation`) |

### Runtime exposure lint

```bash
# Inside the box, dev-sanity checks service binds against posture
SKILLBOX_NETWORK_POSTURE=tailnet_only make dev-sanity
```

## Break-Glass & Recovery

Prefer the DigitalOcean console for recovery because it does not require
reopening public SSH. Public SSH recovery is allowed only as a temporary,
operator-scoped aperture and must be closed before the box is considered back
in posture.

### Droplet console path

1. Identify the droplet and firewall from local inventory:

   ```bash
   BOX_ID=<box-id>
   python3 scripts/box.py status "$BOX_ID" --format json
   doctl compute droplet list --format ID,Name,PublicIPv4
   doctl compute firewall list --format ID,Name,DropletIDs
   ```

2. Open the provider console:

   DigitalOcean Control Panel -> Droplets -> `skillbox-<box-id>` -> Access ->
   Launch Droplet Console.

3. From the console, inspect and repair Tailscale:

   ```bash
   sudo systemctl status tailscaled --no-pager
   sudo tailscale status
   sudo tailscale ip -4
   ```

### Lost or expired Tailscale auth key during `box up`

If provisioning fails before lockdown because the auth key is missing, expired,
or single-use already consumed, the box should still be in `ssh-ready` and the
bootstrap public SSH aperture is expected to remain open. Create a new auth key
in the Tailscale admin console, export it locally, and resume:

```bash
export TAILSCALE_AUTHKEY=tskey-auth-...
python3 scripts/box.py up <box-id> --resume
python3 scripts/box.py posture-proof <box-id>
```

The final proof should show `posture == "tailnet_only"`,
`public_ssh_probe.ok == false`, `tailnet_probe.ok == true`, and no
`violations`.

### Locked-down box loses Tailnet connectivity

Use the droplet console first. With a new Tailscale auth key available, run:

```bash
export TAILSCALE_AUTHKEY=tskey-auth-...
sudo tailscale up \
  --authkey="$TAILSCALE_AUTHKEY" \
  --hostname="skillbox-<box-id>" \
  --ssh \
  --accept-routes=false \
  --accept-dns=false
sudo tailscale status
sudo tailscale ip -4
sudo ufw allow from 100.64.0.0/10 to any port 22 proto tcp comment 'Tailnet-only SSH'
sudo ufw allow in on tailscale0 to any port 22 proto tcp comment 'Tailnet-only SSH (tailscale0)'
sudo ufw status numbered
```

If `ufw status numbered` shows any public `OpenSSH` / `22/tcp` allow rule,
delete that numbered rule from the console:

```bash
sudo ufw --force delete <rule-number>
sudo ufw --force reload
```

Then return to the operator machine and verify:

```bash
python3 scripts/box.py posture-proof <box-id>
python3 scripts/box.py status <box-id> --format json
```

### Tailscale down, need public SSH

If the provider console is unavailable and you must use public SSH, open a
temporary firewall aperture scoped to the operator's current IP. Preserve UDP
41641 so Tailscale can recover:

```bash
BOX_ID=<box-id>
DROPLET_ID=<droplet-id>
FIREWALL_ID=<firewall-id>
OPERATOR_CIDR=<operator-public-ip>/32

doctl compute firewall update "$FIREWALL_ID" \
  --name "skillbox-$BOX_ID" \
  --droplet-ids "$DROPLET_ID" \
  --inbound-rules "protocol:tcp,ports:22,address:$OPERATOR_CIDR;protocol:udp,ports:41641,address:0.0.0.0/0,address:::/0" \
  --outbound-rules "protocol:tcp,ports:all,address:0.0.0.0/0,address:::/0;protocol:udp,ports:all,address:0.0.0.0/0,address:::/0;protocol:icmp,address:0.0.0.0/0,address:::/0"

ssh skillbox@<droplet-public-ip>
```

After repairing Tailscale, re-lock the cloud firewall with the same lockdown
shape that `box.py` uses:

```bash
doctl compute firewall update "$FIREWALL_ID" \
  --name "skillbox-$BOX_ID" \
  --droplet-ids "$DROPLET_ID" \
  --inbound-rules "protocol:udp,ports:41641,address:0.0.0.0/0,address:::/0" \
  --outbound-rules "protocol:tcp,ports:all,address:0.0.0.0/0,address:::/0;protocol:udp,ports:all,address:0.0.0.0/0,address:::/0;protocol:icmp,address:0.0.0.0/0,address:::/0"

python3 scripts/box.py posture-proof "$BOX_ID"
```

`box ssh` may warn "recovery mode only" if it resolves to a public IP for a
`tailnet_only` box. Do not leave that path open; a clean proof must show
`public_ssh_probe.ok == false`.

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

- Do not change posture on a live box without verification. Use
  `posture-proof` for active public-SSH/Tailnet checks and inspect
  `cloud_firewall_rules` for the cloud rule shape.
- `wildcard-direct` binds (`0.0.0.0`) are violations under `tailnet_only`.
  Fix service configs to bind to Tailnet IP or loopback.
- Keep `SKILLBOX_PORT_SENTINEL=observe` until the pulse telemetry is clean;
  `enforce` is intended for dev-server signatures, not arbitrary operator
  sockets.
- The bootstrap aperture (public SSH) exists only during `create` through
  `enroll`. After lockdown, public SSH is break-glass recovery only and a
  clean proof must show it unreachable.
