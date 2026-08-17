# Oracle fleet RPC broker

`runtime_manager.oracle_broker` is the request-admission contract for the
invisible Oracle subagent. The Oracle session credential lives on exactly one
host. Fleet callers never receive it — they send an allowlisted request
document over an authenticated private transport, and the broker decides,
before any browser-facing code runs, whether that request may proceed.

Caller policy and quota live in
[`docs/oracle-policy.md`](oracle-policy.md); attachment validation and private
staging live in `runtime_manager.oracle_attachments`. This page covers the
broker itself: the listener, the wire format, and admission.

The module never opens a socket, spawns a process, or imports a browser driver.
Its only filesystem read is the service-owned local identity described under
[Lanes](#lanes). There is no MCP mirror — that surface is frozen — and the
serving loop belongs to the host that owns the credential. The one agent-facing
command is `manage.py oracle-lane`, which reports a lane and mutates nothing.

## Lanes

Every native Oracle surface routes through one lane contract, so a request
cannot quietly run somewhere the operator did not intend.

| Lane | Proof | Identity |
| --- | --- | --- |
| `fleet` | authenticated tailnet transport + a validated `BindEndpoint` | `tailscale-whois` |
| `local` | a unix-socket peer credential, or a service-owned identity file | `unix-peercred` / `local-service` |

`resolve_lane(...)` decides from the PROOF present, never from a declaration.
Offering proof for both lanes is `lane_ambiguous`; offering neither is
`lane_unavailable`. There is no default lane.

**Identity is never environmental.** `SKILLBOX_ORACLE_CALLER_ID` and its
siblings in `IDENTITY_ENV_OVERRIDE_NAMES` are not read — their *presence* is
refused with `identity_env_override_forbidden`. A caller id taken from the
environment is a quota identity any child process can forge, so a host still
exporting one fails loudly instead of running as someone else. Unset it; enroll
a local identity with `provision_local_identity(state_root, caller_id)` instead.

The local identity file (`<state_root>/oracle/identity.json`) and its parent
must both be owned by the calling uid with no group or other access, and
neither may be a symlink. That is the honest boundary: two processes running as
the same uid are one caller and nothing on a shared host can separate them, but
no process can assert an identity without writing a file only that uid may
write.

`manage.py oracle-lane [--format json] [--state-root PATH]` reports the
resolution the request path would compute. Only the local lane is resolvable
from a CLI — the fleet lane is proven by a transport peer that exists only
inside the serving process — so an unprovisioned host is a refusal with the
enrollment command in `next_actions`, never a silent fallback.

## Three gates, in order

### 1. Listener — `validate_bind_endpoint(host, port)`

Returns a `BindEndpoint` only for a **literal** loopback or Tailscale address.

| Input | Result |
| --- | --- |
| `127.0.0.1`, `::1`, `::ffff:127.0.0.1` | `scope="loopback"` |
| `100.64.0.0/10`, `fd7a:115c:a1e0::/48` | `scope="tailnet"` |
| `0.0.0.0`, `::`, `::ffff:0.0.0.0` | `wildcard_listener_forbidden` |
| `localhost`, any DNS name | `bind_hostname_forbidden` |
| public, LAN, link-local | `public_listener_forbidden` |
| port outside 1024–65535 | `bind_port_forbidden` |

Two details carry real weight:

- **IPv4-mapped wildcards are unwrapped first.** `::ffff:0.0.0.0` binds every
  interface, and only the mapped form reports itself as unspecified.
- **Hostnames are refused outright.** What a name resolves to can change under
  a running listener, so a name can never prove privacy.
- **Loopback is matched before the reserved/multicast check**, because
  `ipaddress` reports `::1` as reserved.

`broker_admission` accepts only a `BindEndpoint` instance. A raw `(host, port)`
tuple is refused with `listener_unverified`, so an unvalidated listener cannot
reach admission at all.

### 2. Request document — `parse_request(payload)`

Exactly these top-level keys, and no others:

```json
{
  "schema": "skillbox.oracle-request.v1",
  "nonce": "<32 lowercase hex>",
  "issued_at": 1735689600,
  "expires_at": 1735689660,
  "mode": "standard",
  "prompt": "…",
  "timeout_seconds": 300,
  "attachments": [
    {"name": "notes.txt", "mime_type": "text/plain", "bytes": 5, "sha256": "…"}
  ]
}
```

Decoding is strict before anything is interpreted: wire budget, UTF-8, real
JSON (`NaN`/`Infinity` refused), unique keys, bounded depth and width.

**Excluded field families** each get their own refusal code, so an operator
learns *why* a field is impossible rather than only that it was unknown. Names
are matched after normalization — case folded, separators removed — so
`browserConfig`, `browser_config`, and `BROWSER-CONFIG` are one entry. The scan
runs at **every depth**, because the top-level allowlist alone would report a
nested `cookies` key as merely unknown.

| Family | Code | Examples |
| --- | --- | --- |
| Hooks | `hooks_forbidden` | `hook`, `hooks`, `callback`, `webhook` |
| Environment | `env_forbidden` | `env`, `environ`, `env_vars`, `dotenv` |
| CDP / devtools | `cdp_target_forbidden` | `cdp_url`, `target_id`, `websocketDebuggerUrl`, `browserWSEndpoint` |
| Browser config | `browser_config_forbidden` | `browserConfig`, `launchOptions`, `chromeFlags`, `userDataDir`, `profile`, `proxy`, `headless` |
| Cookies / credentials | `credential_forbidden` | `cookie`, `cookieJar`, `authorization`, `token`, `apiKey`, `password`, `secret` |
| Executable paths | `executable_path_forbidden` | `exec`, `executablePath`, `binary`, `command`, `argv`, `shell`, `path`, `cwd` |
| Caller-asserted identity | `caller_identity_forbidden` | `caller_id`, `node`, `user`, `login`, `tags`, `acl` |

Identity is deliberately in that list. **The request body has no identity
field at all** — identity comes from the transport, and nothing else.

Attachments carry descriptors, never paths: a `name` restricted to
`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` (no separators, no `..`, no dotfiles, no
control bytes), a mime type from `oracle_attachments.DEFAULT_ALLOWED_MIME_TYPES`,
a byte count, and a SHA-256. `verify_attachment_bytes(descriptor, data)` binds
delivered content to that digest with a constant-time compare. Content is
staged by `oracle_attachments.prepare_attachments` from server-local roots — the
broker never accepts a location from a caller.

### 3. Admission — `oracle_lane_admission(...)`

```python
resolution = resolve_lane(state_root=state_root)      # or transport proof
with oracle_lane_admission(
    payload,
    resolution=resolution,         # LaneResolution, never a raw identity
    policy_engine=engine,          # OraclePolicyEngine
    replay_guard=guard,            # ReplayGuard
) as admission:
    answer = ask_the_oracle(admission.request)   # the ONLY browser contact
write_receipt(admission.receipt.to_payload())
```

This is the single admission path for **both** lanes. The local lane does not
skip a gate just because it never crossed a network: it gets the same document
allowlist, the same freshness and replay defense, the same per-caller quota
reservation, and the same receipt shape. `broker_admission(payload, identity,
endpoint=…)` remains as a fleet-lane façade for the transport, which already
holds a verified peer — it resolves the fleet lane and delegates here, so the
two lanes cannot drift apart.

Every gate raises **before** the `yield`. A server that touches the browser only
inside the `with` body therefore cannot make unauthenticated, unadmitted, or
replayed contact — `tests/test_oracle_broker.py` asserts exactly that with a
sentinel that records each contact. The quota reservation is released on exit,
including when the body raises.

Order: lane resolved → replay store checked → **authority sealed** → document
parsed → freshness → replay → policy reservation → yield.

### The authority cannot be a stand-in

Admission used to accept any object with a callable `admission` attribute. A
library caller could inject a stand-in that yields a syntactically valid grant —
a reservation id, an `admitted_at`, an `expires_at` — and every downstream
check, including the receipt, would read as correct while no per-caller quota,
no enrolled authority journal, and no replay-bound reservation had ever been
consulted. A soft decision shaped like a real one is worse than an outright
failure, because nothing after admission can tell them apart.

There is now no interface to implement. `PolicyAuthority` cannot be subclassed,
cannot be constructed without a module-private seal, and is checked by exact
type, so an instance built with `object.__new__` is refused rather than
crashing. Two sealed factories exist:

| Factory | Kind | `healthy` |
| --- | --- | --- |
| `production_policy_authority(engine)` | `production` | true |
| `sealed_fixture_authority(admission, label=…)` | `fixture` | **false, always** |

`production_policy_authority` accepts only a genuine `OraclePolicyEngine`, by
exact type — a subclass overriding `admission` is the same soft-decision problem
one inheritance hop away. It does not re-verify the enrollment, because
constructing the engine already did: a missing, corrupt, or rolled-back
authority makes construction raise. The `fingerprint` binds to what that
construction proved (policy fingerprint plus canonical state and authority
directories), so two engines pointed at different enrollments are two different
authorities and neither can borrow the other's provenance.

`require_policy_authority` auto-seals a real engine, so existing production
wiring passing `policy_engine=engine` is unchanged. A pretender is refused with
`policy_authority_unsealed` — deliberately distinct from
`policy_engine_unavailable`, because an object that looks like an authority and
is not one is a different event from a `None` that is a wiring mistake.

**Testability is explicit, and default-closed.** A fixture authority flows
through admission unchanged, but `oracle_lane_admission` refuses it unless the
caller passes `require_healthy_authority=False` in so many words. A test cannot
become production wiring by accident, and production wiring cannot be softened
by importing a fixture. The opt-out relaxes *health*, never the seal: an
unsealed authorizer is refused either way.

`admission.authority.health()` renders
`skillbox.oracle-authority-health.v1` — kind, `healthy`, the fingerprint, and
reason codes. It carries no path and no policy body, and a fixture's report can
never be green, so a health endpoint wired to it cannot be made to lie by
injection.

**The honest boundary:** `_AUTHORITY_SEAL` is module-private, so nothing outside
`oracle_broker` can mint an authority — but a caller who can read that name can
already edit the module. This is the same boundary `oracle_policy` documents
under "Honest local authority boundary". The seal defends against a soft
authorizer reaching admission by accident or by library injection, not against
the owner of the source tree.

## Identity

| Constructor | Proof | Caller id |
| --- | --- | --- |
| `peer_identity_from_whois(document, tag_allowlist=…)` | Tailscale LocalAPI whois on the inbound connection | first label of the peer's MagicDNS name |
| `peer_identity_from_peercred(uid, caller_id, allowed_uids=…)` | unix-socket peer credential | supplied by the server, uid allowlisted |

A whois peer must carry at least one **allowlisted ACL tag**. Untagged nodes,
unknown tags, an empty allowlist, and malformed responses are all refused with
`peer_not_allowlisted` — there is no default identity. The resulting
`caller_id` is what `oracle_policy` admits on, and it appears in the receipt.

## Freshness and replay

`ReplayGuard` makes a nonce single-use inside a bounded, self-pruning window:

- `issued_at` within `MAX_CLOCK_SKEW_SECONDS` (60) of now,
- `expires_at > now`, and `expires_at - issued_at <= MAX_TTL_SECONDS` (300).

Bounded memory is safe **only because freshness is enforced first**: an entry
dropped by pruning is provably expired, so re-presenting it fails the freshness
gate rather than passing an empty replay check. When the window is genuinely
full the guard refuses (`replay_capacity_exceeded`) rather than evicting a live
nonce — forgetting a live nonce would open the replay it exists to close.

A nonce replayed with a *different* body is refused separately as
`nonce_reuse_mismatch`: that is a splice attempt, not a retry. Nonces are scoped
to the authenticated caller, so one caller cannot burn another's.

### Durability — `DurableReplayLedger`

`ReplayGuard` lives in one process, so it forgot every claim on restart and two
workers never saw each other's. Both holes hand an attacker a fresh five-minute
window on a captured billable request. `DurableReplayLedger(state_root)` is the
shared record that closes them: one private file
(`<state_root>/oracle/replay-ledger.json`, `0600` in a `0700` uid-owned dir)
serialized by an `flock` on a sibling lock file.

Admission accepts either store — both implement `ReplayDefense` — so a
single-worker deployment pays nothing for durability and a fleet deployment gets
it without a second code path.

```python
guard = DurableReplayLedger(state_root)          # instead of ReplayGuard()
with oracle_lane_admission(payload, resolution=…, replay_guard=guard, …):
    ...
```

Expiry, capacity, and insertion all run inside **one** critical section, so
concurrent workers cannot both win the same nonce and cannot race the pruning
that frees the capacity they are checking. The file is replaced atomically and
the directory is fsynced, so a claim the caller was told was accepted is on disk
before it proceeds.

Fail-closed choices:

| Situation | Result |
| --- | --- |
| unparseable / structurally wrong ledger | `replay_ledger_corrupt` — **never** "treat as empty" |
| ledger or its directory group/other-accessible, or a symlink | `replay_ledger_permissions` |
| lock held past the timeout | `replay_ledger_locked` (never an unbounded wait) |
| window genuinely full | `replay_capacity_exceeded`, not eviction of a live nonce |
| missing ledger | a fresh ledger — deleting it needs the uid that owns it |

The ledger stores only caller id, nonce, expiry, and the request digest. No
prompt text and no attachment content ever reach it; a test greps the file to
keep it that way.

## Receipts

`BrokerReceipt.to_payload()` renders `skillbox.oracle-receipt.v1`: lane, caller
id, auth method, whois node, endpoint and scope, mode, reservation id, request
digest, sizes (`prompt_bytes`, `file_count`, `attachment_bytes`), timeout, and
the admission window. It carries **no prompt text and no attachment content** —
a test pins that.

One receipt shape covers both lanes, so a fleet request and a local request are
audited identically; `lane` and `auth_method` carry the provenance, and
`endpoint`/`scope` are empty on the local lane because there is no listener to
name. Metrics are a separate contract: `runtime_manager.oracle_metrics` records
no caller identity at all, deliberately, so reliability data can never become a
correlation handle for an account. Identity belongs in the receipt; never in a
metric.

## Refusal codes

Refusals raise `OracleBrokerError`, a `runtime_manager.errors.ValidationError`,
so `to_payload()` renders the standard typed envelope. The message is the
constant `"oracle broker: refused"` and the context is empty by construction: a
refusal must never echo prompt text, a digest, or a peer address.

`REFUSAL_CODES` is the complete set of broker-owned codes; a test asserts every
`_refuse("…")` literal in the module is declared there. Denials from
`oracle_policy` (`caller_denied`, `file_count_exceeded`, `byte_quota_exceeded`,
`concurrency_exceeded`, …) are re-raised as `OracleBrokerError` with their own
code preserved. Two codes are shared by both layers on purpose —
`prompt_too_large` and `request_too_large` — because the wire ceiling and the
per-caller ceiling mean the same thing to a caller.

## Ceilings

| Constant | Value | Note |
| --- | --- | --- |
| `MAX_REQUEST_BYTES` | 5 MiB | whole document |
| `MAX_PROMPT_BYTES` | 4 MiB | mirrors the policy ceiling |
| `MAX_ATTACHMENTS` | 32 | mirrors the policy ceiling |
| `MAX_ATTACHMENT_BYTES` | 256 MiB | mirrors the policy ceiling |
| `MAX_TIMEOUT_SECONDS` | 21 600 | mirrors the policy ceiling |
| `MAX_DOCUMENT_DEPTH` / `MAX_DOCUMENT_KEYS` / `MAX_DOCUMENT_ITEMS` | 6 / 32 / 64 | anti-blowup bounds |

These are the *wire* ceilings; a caller's effective limits are always the
tighter of these and its policy entry. A test pins the mirrored values against
`oracle_policy`, so the two cannot drift apart.

## The client half: canonical fleet targets

`runtime_manager.oracle_fleet` is the other side of this contract — what a
fleet machine runs to reach the broker. It exists because the fleet had two
spellings and one of them resolved nowhere: `d3` was the devbox lane, and `d3c`
was the operator's shorthand for the conference lane that was not a target
anywhere in the tree, so those invocations were hand-rolled. That is how a lane
acquires its own listener, its own retry rule, and eventually its own security
posture.

There are exactly two canonical targets, `d3` and `d3c`, and everything else is
an alias onto one of them:

| Canonical | Aliases | Lane |
| --- | --- | --- |
| `d3` | `d`, `default`, `devbox` | Linux tailnet devbox |
| `d3c` | `c`, `conf`, `conference`, `conference1`, `conference1-wsl`, `d3-c`, `d3-conference`, `wsl` | WSL conference — see [`docs/conference1.md`](conference1.md) |

`resolve_target()` normalizes NFKC, case, separators, and padding, then looks
the result up in a closed table. An unknown spelling refuses with
`fleet_target_unknown` and **no did-you-mean**: a near-miss on a fleet target
resolves to somebody's actual machine, and the refusal must not echo the
caller's string back either.

### Targets bind to machines by capability, never by hostname

A canonical target does not name a host. It declares a capability predicate,
and `MachinesConfig.require_one_by_caps()` resolves it against the operator's
private `machines.yaml`:

| Target | Required caps | Trust |
| --- | --- | --- |
| `d3` | `os:linux`, `tailnet`, `docker` | `allowlisted` |
| `d3c` | `os:wsl`, `docker` | `allowlisted` |

Zero matches and several matches are both errors (`fleet_machine_unresolved`) —
a fleet target that resolves ambiguously is exactly the drift this is here to
catch, so it never silently picks the first one. The upshot is that real fleet
identity stays in the private registry: the tracked tree holds predicates. A
test proves it by binding against a fixture whose machine ids share no
substring with any target or alias.

### One contract, both targets

`plan_invocation()` builds a `FleetInvocation` and refuses before it is usable:
the listener goes through `validate_bind_endpoint`, the target resolves to
exactly one machine, and the rendered document is parsed by `parse_request`
*at plan time* — so a forbidden field is found on the client, not discovered
against a live host. A test asserts the two targets' plans are identical apart
from `target` and `machine_id`.

Attachments are content-addressed (`name`, `mime_type`, `bytes`, `sha256`).
`TransferPlan` has no path field at all, so where a client stages files and
writes its result is a local fact that cannot be transcribed onto another host.
Results are accepted only after `verify_result()` matches both size and digest,
and a zero-byte result is never evidence of a completed run.

### Recovering from tunnel loss without weakening replay defense

`invoke()` retries, but narrowly:

- only `FleetTransportLost` retries, and a transport may raise it **only** when
  it is certain the request produced no response. A tunnel that died mid-reply
  is not this, because retrying it could duplicate a side effect.
- a broker refusal is terminal. Re-sending a request the host *rejected* is how
  a client turns a policy denial into a quota attack.
- every attempt mints a fresh nonce and fresh timestamps. This is not a detail:
  the replay guard is single-use, so re-sending a dropped request under its
  original nonce would be refused as a replay rather than retried. Fresh
  nonces are what make recovery possible without loosening the guard.
- once a response exists, no further attempt is made.

### There is no credential in the contract

Nothing in `FleetInvocation`, `TransferPlan`, `TransferFile`, `ResultEnvelope`,
or `FleetAttempt` can hold a token, cookie, profile, or key — a test asserts the
field names. Identity is what the transport already proved (Tailscale whois or
unix peercred) and the broker re-derives on its own side. So `render_argv()`
carries no `-i identity_file`, no `--user-data-dir`, no
`--remote-debugging-port`, and the request goes over stdin.

`fleet_security_audit()` decides the failure gate from the rendered contract
rather than from that claim — the documents, receipts, and argv are scanned as
text, so a field added later that reintroduces a cookie path or a CDP URL fails
the audit even if nobody updates it. Gates: `wildcard_listener`,
`raw_cdp_exposure`, `remote_hook_or_browser_config`, `cookie_profile_transfer`,
`argv_token`, `unauthenticated_browser_contact`, `single_client_contract`.

## Validation

```bash
PYTHONPATH=.env-manager python3 -m unittest \
  tests.test_oracle_broker tests.test_tailnet_only_regression
PYTHONPATH=.env-manager python3 -m unittest \
  tests.test_oracle_lane_parity tests.test_oracle_metrics
PYTHONPATH=.env-manager python3 -m unittest \
  tests.test_oracle_fleet tests.test_machines
python3 -m ruff check \
  .env-manager/runtime_manager/oracle_broker.py \
  .env-manager/runtime_manager/oracle_fleet.py
```

`tests/test_oracle_lane_parity.py` proves the surfaces agree: the CLI, the
operator MCP server's script-dispatch contract, and an in-process caller all
resolve the same lane from the same state — including on a caller with no
browser anywhere on `PATH`.

The listener invariants are duplicated into
`tests/test_tailnet_only_regression.py` on purpose: the broker fronts the one
host holding the Oracle credential, so "no wildcard listener" is a posture
invariant, not just a module detail.

### Executable proof

```bash
PYTHONPATH=.env-manager python3 tests/proof_oracle_fleet.py \
  --targets d3,conference1-wsl --out /tmp/oracle-subagent-e2e/FINAL
jq -e '.hard_gates == "pass"' /tmp/oracle-subagent-e2e/FINAL/fleet-manifest.json
```

`tests/proof_oracle_fleet.py` drives both halves in one process: the fleet
client sends real request documents into real `broker_admission` with a real
`OraclePolicyEngine` and `ReplayGuard`. Nothing on the security path is stubbed,
and one target is exercised through a deliberately dropped tunnel so recovery
is demonstrated by an artifact rather than claimed in prose. It writes
`fleet-manifest.json`, `d3/receipt.json`, `d3c/receipt.json`, and
`fleet-security-audit.json`.

The harness is offline by contract — no network, no SSH, no Docker, no browser.
`hard_gates` therefore covers the security failure gate, all of which is
decidable offline. Criteria that genuinely need a live host (an actual file
transfer to the target, an actual dropped tailnet tunnel) are reported
separately in the manifest's `local_criteria` with an explicit
`live_fleet_gap`, rather than being asserted from a local run.
