# Conference1 (`d3c`) — the WSL lane

`d3c` is the canonical name for the Conference1 WSL machine across the Oracle
fleet contract. This page exists because that name did not resolve anywhere for
a while: `d3` was a target, `d3c` was operator shorthand, and invocations
against it were hand-rolled. Hand-rolled lanes drift — they grow their own
listener, their own retry rule, and eventually their own security posture — so
`d3c` is now a first-class target over the same code path as `d3`.

Real host metadata for this machine lives in the operator's private registries
(`machines.yaml`, and `SKILLBOX_CLIPBOARD_HOSTS` for the clipboard bundle). The
tracked tree holds names and predicates, never addresses.

## One vocabulary, two subsystems

The operator says "conference1" or "d3c" and means one machine. Two subsystems
resolve that phrase in their own namespaces, and they agree on the destination:

| You type | Oracle fleet target | Clipboard profile |
| --- | --- | --- |
| `d3c`, `d3-c`, `d3-conference` | `d3c` | `conference1` |
| `conference1`, `conference1-wsl` | `d3c` | `conference1` |
| `conf`, `conference`, `c` | `d3c` | `conference1` |
| `conference1-ssh` | *(not a fleet target)* | `conference1-fallback` |
| `wsl` | `d3c` | *(no profile)* |

The Oracle side is `runtime_manager.oracle_fleet.TARGET_ALIASES`; the clipboard
side is the `aliases` table in `scripts/clipboard/hosts.json`. They are separate
tables on purpose — the clipboard bundle has a fallback profile the Oracle lane
has no equivalent for — but every spelling an operator actually types lands on
the same box in both.

`sbp conference1` remains the read-only status/Serve-URL surface for this host;
see AGENTS.md. It is unrelated to Oracle admission and unaffected by any of
this.

## How `d3c` resolves to a machine

`d3c` does not name a host. It declares a capability predicate, and
`MachinesConfig.require_one_by_caps()` resolves it against the private
`machines.yaml`:

```
caps:  os:wsl, docker
trust: allowlisted
```

Exactly one machine must match. Zero is `fleet_machine_unresolved`; so is two —
an ambiguous fleet target must never silently pick the first candidate, because
the thing it is picking is a machine that will receive a request.

Practical consequence: if `d3c` stops resolving after a registry edit, the fix
is in `machines.yaml` (the WSL box lost `docker`, or a second WSL box was
added), not in the fleet module.

## What `d3c` and `d3` share

Everything except the target name and the resolved machine. Same request
builder, same listener validation, same content-addressed transfer plan, same
retry policy, same audit. A test asserts the two rendered plans are identical
apart from `target` and `machine_id`, and the proof harness re-checks it at run
time and records the result in `fleet-manifest.json`.

That matters most for the WSL lane specifically. WSL is the environment most
likely to tempt someone into a one-off — a different tunnel, a Windows-side
browser, a hand-copied profile directory. The contract gives that temptation
nowhere to land: there is no credential field, no path field, and no browser
configuration field to populate.

## Tunnel loss

The conference lane is the flakiest transport in the fleet, so it is the one the
proof harness deliberately breaks:

```bash
PYTHONPATH=.env-manager python3 tests/proof_oracle_fleet.py \
  --targets d3,conference1-wsl --out /tmp/oracle-subagent-e2e/FINAL
```

The second target's first attempt drops the tunnel *before* the host is
reached, and the client recovers on the next attempt with a **fresh nonce**.
The nonce matters: the broker's replay guard is single-use, so re-sending the
dropped request under its original nonce would be refused as a replay rather
than retried. `d3c/receipt.json` records both attempts, their distinct request
digests, and `recovered_from_transport_loss: true`.

What is proven locally is the recovery *policy*. Recovery against a genuinely
dropped tailnet or SSH tunnel to the live box is not asserted by this harness —
it is offline by contract — and the manifest says so in `local_criteria` under
`live_fleet_gap`.

## Failure gate

Every hard gate in [`docs/oracle.md`](oracle.md) applies here unchanged, and
`fleet-security-audit.json` decides them from the rendered contract rather than
from a claim:

- no wildcard listener — `d3c`'s endpoint passes `validate_bind_endpoint`, so it
  is loopback or tailnet, never `0.0.0.0` and never a hostname;
- no raw CDP/devtools/websocket endpoint in the contract, receipts, or argv;
- no hook, `browserConfig`, env, or executable path — the broker's allowlist
  refuses those families at every depth;
- no cookie or profile transfer — attachments are digests, not paths;
- no token in argv — there is no credential field to render;
- no unauthenticated browser contact — every receipt names a transport-proved
  auth method.

## Related

- [`docs/oracle.md`](oracle.md) — the broker and the client contract.
- [`docs/oracle-policy.md`](oracle-policy.md) — caller policy and quota.
- [`docs/oracle-metrics.md`](oracle-metrics.md) — latency and reliability view.
- [`docs/clipboard-bootstrap.md`](clipboard-bootstrap.md) — the clipboard
  bundle's own `conference1` profile and its fallback.
