# Oracle host Chrome sandbox posture

The cookie-bearing canonical Chrome on the Oracle host runs with
`--no-sandbox`. That flag turns off the renderer sandbox on the one process on
the estate holding a live, authenticated ChatGPT session, so a renderer
compromise reaches the profile directory and its cookies directly.

**Restoring the browser sandbox is the preferred outcome.** Everything below is
the fallback for a host that genuinely cannot support one, and it is written so
that the fallback can never be mistaken for the fix.

`runtime_manager.oracle_sandbox` evaluates the posture and the
`oracle_browser_sandbox` gate in `sbp doctor` reports it.

## No false green

This is the property the module exists to guarantee:

> No combination of waivers, controls, or evidence makes `--no-sandbox`
> evaluate to `enforced`, or to a `pass` verdict.

`tests/test_oracle_sandbox.py` asserts it exhaustively — all 16 control
combinations × 4 waiver conditions × 4 sandbox-mechanism combinations, 256
cases — rather than by sampling. A waived host reports `waived`, never `pass`.

| State | Verdict | Meaning |
| --- | --- | --- |
| `enforced` | `pass` | Chrome runs its own sandbox. The only green. |
| `waived` | `inconclusive` | Sandbox off, exception live, **all four** controls verified. |
| `uncontained` | `fail` | Sandbox off and the exception is missing, expired, foreign, or under-compensated. |
| `undeclared` | `inconclusive` | No declaration — this box is not the Oracle host. |

`waived` maps to `inconclusive` rather than `pass` or `fail` on purpose. The
doctor cannot certify a sandbox that is switched off, so it declines to say
green; and a permanent red for an accepted, scheduled exception would train
operators to skip the gate, which is its own kind of false signal. `inconclusive`
is visible in every run and does not flip the exit code.

Two more fail-closed rules:

- A declaration that is **present but malformed** is `fail`, never
  `inconclusive`. A broken declaration on the host that matters most is a
  finding, not a missing one.
- `--no-sandbox` absent but no sandbox mechanism available is `uncontained`
  (`sandbox_unavailable`) — Chrome was launched expecting a sandbox the kernel
  cannot give it, which is a broken host, not a clean one.

## The four compensating controls

All four, or the exception is not accepted. Each must carry a short, non-secret
evidence token; **`verified: true` with no evidence is refused, not believed** —
an unevidenced claim is exactly how a containment check goes quietly green.

| Control | What it means | Example evidence |
| --- | --- | --- |
| `single_service_uid` | The browser and its profile are owned by one dedicated service uid, not a human login. | `uid:oracle-svc` |
| `no_shared_interactive_logins` | No shared or interactive shell reaches that uid. | `sshd:DenyUsers=oracle-svc` |
| `hardened_unit_isolation` | The unit runs with kernel-enforced isolation. | `systemd:ProtectSystem=strict` |
| `bounded_filesystem_access` | The browser sees only its own profile and runtime tree. | `systemd:ReadWritePaths=1` |

A missing entry is refused rather than treated as unverified: a partial report
must never be accepted as a full one.

## The waiver

A dated, host-scoped, operator-approved exception — not a policy.

- `host` must match the evaluating host. A waiver approved for one box never
  covers another.
- `expires_at_ms` is required, must be after `approved_at_ms`, and may be at
  most **90 days** later. An exception with no practical end date is a policy
  change wearing a waiver's clothes.
- `reason` comes from a closed vocabulary: `userns_unavailable`,
  `kernel_lockdown`, `container_runtime_restriction`,
  `vendor_binary_limitation`.
- Expiry is exclusive: a waiver whose deadline is exactly now is expired.

## The declaration

A host-side collector writes `<state-root>/oracle/sandbox-posture.json`, mode
`0600`, uid-owned, not a symlink:

```json
{
  "schema": "skillbox.oracle-sandbox-posture.v1",
  "host": "d3",
  "evidence": {
    "no_sandbox_flag": true,
    "user_namespaces_available": false,
    "setuid_sandbox_present": false
  },
  "controls": {
    "single_service_uid":            {"verified": true, "evidence": "uid:oracle-svc"},
    "no_shared_interactive_logins":  {"verified": true, "evidence": "sshd:DenyUsers=oracle-svc"},
    "hardened_unit_isolation":       {"verified": true, "evidence": "systemd:ProtectSystem=strict"},
    "bounded_filesystem_access":     {"verified": true, "evidence": "systemd:ReadWritePaths=1"}
  },
  "waiver": {
    "host": "d3",
    "reason": "userns_unavailable",
    "approved_by": "operator",
    "approved_at_ms": 1760000000000,
    "expires_at_ms": 1762592000000
  }
}
```

Omit `waiver` and the host is `uncontained` — which is the correct reading of
"the sandbox is off and nobody has signed for it".

Evidence tokens are pattern-bounded to ~96 characters of
`A-Za-z0-9._:=/+-`, so a doctor line can never carry a host path or a secret.

## Reading the gate

```bash
python3 .env-manager/manage.py structure-doctor --format json \
  | python3 -c 'import json,sys; [print(g["status"], g["detail"]) for g in json.load(sys.stdin)["gates"] if g["name"]=="oracle_browser_sandbox"]'
```

On a box that is not the Oracle host:

```
inco   no oracle sandbox declaration on this box (not the oracle host)
```

On a waived Oracle host the detail says `DISABLED` out loud, names the count of
verified controls, and prints the waiver expiry — a test asserts the word
`enforced` never appears in it.

## Host-side work this does not do

The evaluator reports; it does not change the host. Restoring the sandbox, or
standing up the controls that justify an exception, is Oracle-host work:

1. Remove `--no-sandbox` from the host-local Chrome wrapper referenced by
   `CHROME_BIN` (`launch-chatgpt-cdp.sh:434` launches through it), and confirm
   Chrome starts with `user.max_user_namespaces > 0` or a working setuid
   sandbox binary.
2. If it cannot start, keep the flag, stand up all four controls, and have the
   collector write the declaration above with a signed, dated waiver.
3. Re-run `sbp doctor` and confirm the gate reads `waived` with a live expiry —
   not `uncontained`, and never `pass`.
