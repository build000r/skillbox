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
| enroll | Same | Tailscale joined; the box's Tailnet identity is proven. **Nothing is closed here.** |
| lockdown | Tailscale UDP only (cloud firewall updated) | Tailnet reachability proven, host UFW verified, cloud firewall drops public TCP 22, re-read verifies it |
| deploy | Tailnet only | Release installed over Tailscale SSH |
| ready | Tailnet only | Box operational; public SSH = policy drift |

`enroll` and `lockdown` are two separate stages, in the real run and in the
`--dry-run` preview alike. They were once a single function, and that shape
caused the bug this split fixes: a resume that skipped enrollment also skipped
lockdown, and deployed to a box with port 22 open to the world. Read the step
list in any `box up` output as the authority — it is now the same list either
way:

```
create → storage → bootstrap → ssh-ready → enroll → lockdown → deploy
       → contract → launch → first-box → verify
```

After lockdown, the only intended inbound path is Tailscale. The DigitalOcean
firewall keeps inbound UDP 41641 for Tailscale and drops public TCP 22; host
UFW accepts SSH from the Tailnet CIDR / `tailscale0`. Public SSH reachability
after this point is drift or break-glass recovery state. All subsequent
`box ssh`, `box status`, and deploy commands prefer Tailscale IP or MagicDNS
hostname.

### Enrollment and lockdown checkpoints

Each stage advances only on evidence it produced itself. Neither stage trusts
inventory, and neither trusts the other's exit code.

| Checkpoint | Required evidence | If missing |
|---|---|---|
| enroll → `lockdown` state | `scripts/02-install-tailscale.sh` exits 0 **and** prints `TAILSCALE_IPV4=<addr>` **and** the address is inside `100.64.0.0/10` | Refuse. Any recorded `tailscale_ip` is **cleared**, so a later resume cannot read it back as proof |
| lockdown: identity | inventory `tailscale_ip` still validates as a Tailnet address | `tailnet_identity_missing` |
| lockdown: reachability | operator-side SSH to the box over its Tailnet address succeeds | `tailnet_unreachable` |
| lockdown: cloud firewall | a `cloud_firewall_id` exists on the box | `cloud_firewall_missing` |
| lockdown: mutation | `doctl compute firewall update` succeeds | `cloud_firewall_update_failed` |
| lockdown: re-read | the firewall reads back | `cloud_firewall_reread_failed` |
| lockdown: identity of the evidence | the re-read's `id` equals the requested firewall id | `cloud_firewall_identity_mismatch` |
| lockdown: result | no inbound TCP 22 from `0.0.0.0/0` or `::/0` | `cloud_firewall_public_ssh_open` |

Two ordering rules are load-bearing:

- **Reachability is proven before the firewall is touched.** Verifying it
  afterwards would mean discovering the Tailnet path is dead at the moment the
  public path is already gone. Failing this check leaves the box in `lockdown`
  with public SSH still open — recoverable. The inverse is not.
- **The firewall is read before it is written.** A resume must re-prove
  lockdown, but re-proving is a read. An already-closed firewall reports
  `already-locked-down` and is not re-mutated; `box up --resume` never
  blind-retries a mutation.

Every one of those failures leaves the box in `lockdown` and emits its own
`error.type` in the `box up` JSON payload, so an agent can branch on the exact
refusal instead of pattern-matching a message. A failed lockdown is never reset
to `ssh-ready`.

### Resume behaviour

`box up <id> --resume` is dispatched from durable state, never from a leftover
Tailnet IP. Resumable states are `ssh-ready`, `lockdown`, `deploying`,
`acceptance`, `onboarding`; any other state is refused with `invalid_state`.

| State on resume | enroll | lockdown |
|---|---|---|
| `ssh-ready`, no valid `tailscale_ip` | re-enrols | runs |
| `ssh-ready`, valid `tailscale_ip` | accepts the recorded address as legacy enrollment evidence, then advances into `lockdown` | **runs** |
| `lockdown` | skipped — reaching this state is the proof | **runs** (re-proves; skips the mutation if already closed) |
| `deploying`, `acceptance`, `onboarding` | skipped | skipped — a later state is the proof |

The second row is the one to know. An `ssh-ready` box carrying a Tailnet IP used
to skip straight to deploy; the address is still honoured as evidence of
*enrollment*, but it says nothing about whether port 22 is shut, so lockdown
still has to pass.

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
export SKILLBOX_TS_AUTHKEY=tskey-auth-...
python3 scripts/box.py up <box-id> --resume
python3 scripts/box.py posture-proof <box-id>
```

`box.py` reads `SKILLBOX_TS_AUTHKEY` (the same name `.env.example` declares) and
injects it into the host script as `TAILSCALE_AUTHKEY`. Export the operator-side
name; the host-side name is not read from your shell.

The final proof should show `posture == "tailnet_only"`,
`public_ssh_probe.ok == false`, `tailnet_probe.ok == true`, and no
`violations`.

If the resume fails at `lockdown` rather than `enroll`, the box keeps a proven
Tailnet identity and stays in `lockdown` with public SSH still open. Read the
`error.type` to know which checkpoint refused, repair that, and re-run the same
`--resume` command — it re-proves lockdown without re-enrolling and without
re-issuing the firewall mutation.

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

### Real teardown is identity-bound

A real teardown requires naming the box it destroys. There is no blanket
shortcut on the Make surface, because a bare truthy flag would let a caller
confirm a destruction without ever spelling out the target.

```bash
# preview — the only unconfirmed path
python3 scripts/box.py down <box-id> --dry-run --format json
make box-down BOX=<box-id> DRY_RUN=1

# real teardown — the confirmation must equal the box id
python3 scripts/box.py down <box-id> --confirm <box-id> --format json
make box-down BOX=<box-id> CONFIRM=<box-id>
```

Two gates stand in front of a real teardown, and both are observable without
touching infrastructure:

| Refusal | `error_code` / `error.type` | Meaning |
|---|---|---|
| no `--confirm` and no `--yes` | `confirmation_required` | Real teardown was requested with no confirmation at all |
| `--confirm` present but ≠ box id | `confirmation_required` | The confirmation names a different box; it is not repaired for you |
| repository has uncommitted changes | `dirty_tree_refused` | Destructive operations run from a committed state only |

`make box-down` is a pass-through, not a gate: `DRY_RUN` selects the preview
flag and `CONFIRM` forwards the operator's value verbatim, so `scripts/box.py`
is still the thing that decides. `--yes` remains accepted on the CLI for
non-interactive callers, but the Make and MCP surfaces deliberately do not
expose it.

The operator MCP `operator_teardown` tool (deprecated in favour of the CLI)
requires `dry_run=true` first; the resulting marker is bound to `box_id`, and a
real call then forwards `--confirm <box_id>` so a wrong id fails at the CLI's
exact-match check rather than being waved through by the wrapper.

### Teardown truth invariant

A fleet inventory that says `destroyed` while a droplet still bills is the most
expensive lie. `box down` therefore never trusts the `doctl ... droplet delete`
exit code alone: after the delete call it issues a bounded read-after-delete
confirmation (`doctl compute droplet get <id> --output json`) and only writes
`destroyed` once the droplet is observed absent. DigitalOcean's delete is
eventually consistent, so the confirm makes **at most 3 bounded read attempts
with linear backoff** (2s, then 4s) and then lands in a truthful pending state —
it never spins or hangs.

Tailscale removal is best-effort: a failed `tailscale logout` is reported as a
`remove` step `warn` but never blocks droplet destruction.

### Provider observations

Every read-after-delete returns a typed observation rather than a boolean, so
"the droplet is gone" and "we could not find out" can never collapse into the
same answer. The five outcomes are:

| `ProviderOutcome` | What the provider said | Retried? | May write `destroyed` |
|---|---|---|---|
| `found` | The droplet is still API-listed | Yes, up to the attempt bound | **No** |
| `confirmed-not-found` | Explicit not-found evidence | n/a — returns immediately | **Yes** |
| `retryable-failure` | Transient error, or the read raised | Yes, up to the attempt bound | No |
| `permanent-failure` | The provider refused in a way retrying cannot fix | No — stops immediately | No |
| `malformed-response` | Unparseable, or the reader violated its own contract | No — stops immediately | No |

**The sole advancement rule: only `confirmed-not-found` may write
`destroyed`.** Every other outcome — including a read that simply failed — parks
the box in `destroy-pending`. Absence of evidence is not evidence of absence,
and the billing meter does not care which one you had.

The observation is attached to the `confirm` step in the `box down` payload
(`steps[].provider_observation`) and echoed at the top level on a pending
result, so the reason is auditable after the fact.

One skip is deliberate: a box with no `droplet_id` returns
`confirmed-absent` without a provider call. There is no droplet to bill, so
absence is vacuously confirmed.

### Still-found versus confirmation-unavailable

Both land in `destroy-pending`, and both carry billing risk — but they are
different operator situations and the message distinguishes them:

| Situation | Outcome | Message reads | What to do |
|---|---|---|---|
| The droplet is still there | `found` | "DigitalOcean still lists the droplet" | Re-run; eventual consistency usually resolves it |
| We could not establish absence | `retryable-failure`, `permanent-failure`, `malformed-response` | "provider confirmation is unavailable (`<outcome>`)" | Re-run to observe again; if it persists, check the droplet and your provider access in the DO console |

Re-running is a fresh observation, not a fresh delete: `box down` from
`destroy-pending` re-confirms absence and never re-issues the destroy call.

### Teardown states

| State | Meaning | Billing risk | Reachable next state(s) | Retry |
|-------|---------|--------------|-------------------------|-------|
| `draining` | Services stopped; tailnet/firewall/droplet teardown in progress | Possible (droplet may still exist) | `destroy-pending`, `volume-cleanup-failed`, `destroyed` | `box down <id> --confirm <id>` |
| `destroy-pending` | Droplet delete was requested but absence is unproven — either **still API-listed** or **confirmation unavailable** | **Yes** — droplet may still bill; inventory deliberately does NOT say `destroyed` | `destroy-pending`, `volume-cleanup-failed`, `destroyed` | `box down <id> --confirm <id>` (re-confirms absence; never re-deletes) |
| `volume-cleanup-failed` | Droplet **confirmed gone**, but the attached volume could not be detached/deleted | No — droplet is gone | `volume-cleanup-failed`, `destroyed` | `box down <id> --confirm <id>` (retries volume cleanup only) |
| `destroyed` | Droplet confirmed absent and volume cleanup complete (or no volume) | None | terminal | n/a |

Both `destroy-pending` and `volume-cleanup-failed` are surfaced in
`box status <id>` and `box list` (a `teardown_pending` block carrying the exact
identity-confirmed retry command and a `billing_risk` flag), not just in the
output of the `box down` command that produced them. Re-running `box down` from
either state is idempotent and converges to `destroyed` once the underlying
infrastructure cooperates.

> Registered/external boxes (`management_mode: external`) have no managed
> droplet to confirm and are out of scope for teardown — use `box unregister`.

## How this contract is proven

Everything on this page is proven against fixtures, never against live
infrastructure. No test in this repository creates a droplet, joins a tailnet,
mutates a cloud firewall, or destroys anything: provider calls, `doctl`, SSH,
and the inventory file are all mocked, and the suites run offline.

```bash
python3 -m unittest tests.test_box tests.test_box_lifecycle \
    tests.test_box_state_machine tests.test_tailnet_only_regression \
    tests.test_operator_mcp_server
```

| Claim on this page | Where it is pinned |
|---|---|
| Enrollment evidence rules; the stale-IP clear | `tests.test_box_state_machine.EnrollmentEvidenceTests` |
| Each lockdown checkpoint and its typed error | `tests.test_box_state_machine.CloudFirewallFailClosedTests` |
| Resume dispatch per state; failed lockdown never deploys | `tests.test_box_state_machine.LockdownRecoveryTests` |
| Real and preview step order agree | `tests.test_box_state_machine.LockdownStageOrderTests` |
| Identity-bound teardown across CLI, Make, and MCP | `tests.test_box.TeardownSurfaceParityTests`, `tests.test_operator_mcp_server` |
| Every teardown-recovery hint is runnable as printed | `tests.test_box_state_machine.TeardownRecoveryHintTests` |
| Only `confirmed-not-found` advances to `destroyed` | `tests.test_box_state_machine`, `tests.test_box` |

The refusal paths are also observable by hand without risk, because both
teardown gates fire before any provider call. Against a box id that does not
exist:

```bash
python3 scripts/box.py down ghost --format json                  # confirmation_required
python3 scripts/box.py down ghost --confirm wrong --format json  # confirmation_required (mismatch)
make box-down BOX=ghost DRY_RUN=1                                # preview path, no confirmation
```

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
- A box parked in `lockdown` still has public SSH open. That is the intended,
  recoverable side of a failed lockdown — but it is not a resting state. Repair
  the checkpoint named in `error.type` and re-run `box up <id> --resume`.
- Do not "fix" a `destroy-pending` box by hand-editing inventory to
  `destroyed`. The state is the honest answer to an unproven absence; editing it
  converts a billing risk you can see into one you cannot.
