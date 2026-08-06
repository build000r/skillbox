# Skillbox Amp project-Orb contract

Status: accepted repository-owned contract. Local implementation and fixture
proof do not constitute acceptance by Sweet Potato/SPAPS or authorization to
deploy production.

This document specializes the durable-box [product vision](../VISION.md) for a
disposable Amp project Orb. The Orb is an execution client of Skillbox, not a
second Skillbox host or control plane.

## Boundary

The project Orb is one checkout of `build000r/skillbox`, one Amp project
identity, and one thread. It may inspect, test, and change this repository and
use explicitly authorized read-only remote services. It may be paused,
replaced, or deleted without losing authoritative policy, skills, releases, or
production evidence.

An ordinary project Orb is not:

- the durable Skillbox box, operator machine, secret store, or scheduler;
- a global Amp skill home or a new SBP source/policy/lock lifecycle;
- a Sweet Potato host or a local SPAPS stack;
- a multi-project, campaign, or skill-family delivery system; or
- a production deployment or infrastructure administration authority.

Authoritative changes must be committed to Git or written by an independently
authorized external system with its own receipt. Tool or credential presence
does not grant authority.

## Offline bootstrap and readiness

Tracked hooks [`.agents/setup`](../../.agents/setup) and
[`.agents/resume`](../../.agents/resume) are bounded, idempotent, and offline.
They do not install packages, join a Tailnet, read operator `.env` files,
repair external systems, start services, or run background work.

Setup checks local commands and disk capacity, runs bounded `compileall` and a
focused unit smoke, creates a stable private resume identity, and evaluates
the tracked [capability declaration](../../.agents/orb-capabilities.json).
Resume performs only the fast local capacity, identity, and readiness checks.
GNU `timeout` supplies a hard elapsed bound to every subprocess invoked by the
hooks; planted timeout fixtures verify typed exits.

Hook failures use these exit classes:

| Exit | Class | Examples |
|---:|---|---|
| 10 | `setup` | unexpected hook failure |
| 20 | `dependency` | missing Python, Git, or `timeout` |
| 30 | `capacity` | insufficient fixed Orb disk headroom |
| 40 | `auth` | reserved for an explicitly requested auth operation |
| 50 | `validation` | compile, test, identity, or readiness failure/timeout |

The offline readiness evaluator emits only capability IDs, classes, states,
and reason codes. Its states are:

- `ready`: required local capability is usable;
- `configured`: optional configuration names are present (not authenticated);
- `degraded`: optional configuration is absent;
- `blocked`: required local capability is absent; and
- `forbidden`: ordinary Orb authority must never include the capability.

Receipts use schema `skillbox.amp-project-orb.readiness/1`, always state
`network_attempted: false` and `external_readiness_claimed: false`, and are
atomically written with mode `0600`. Hook status receipts use
`skillbox.amp-project-orb.hook-status/1`. Environment values, tokens, private
destinations, and raw command output are never copied into either receipt.

Manual local evaluation is:

```bash
python3 scripts/orb/orb_readiness.py collect --context manual
python3 scripts/orb/orb_readiness.py collect \
  --context manual \
  --output .skillbox-state/project-orb/hook-state/orb-readiness.json
```

## Amp is the third project-local SBP projection

SBP's existing source selection, picks, dispatcher policy, repository
overrides, locks, and lifecycle remain the only skill lifecycle. For a project
target, the one selected source is projected to three sibling destinations:

```text
selected locked source
  ├── .claude/skills/<name>
  ├── .codex/skills/<name>
  └── .agents/skills/<name>
```

The Amp adapter adds only `.agents/skills` destination enumeration, inventory,
unlink attribution, and lifecycle parity. It has no global Amp target. Dry-run
does not create directories, real directories are never overwritten, reruns
are idempotent, and remove/prune/unlink operate through the same SBP decisions
as Claude and Codex. Discovery stops at the nearest repository boundary.

No skill source bytes are vendored into `.agents/skills`; project projections
are links to the selected source. There is no local, multi-project, campaign,
or family fallback, and this project does not take ownership of family
delivery.

## Amp workload identity and sbpd authorization

Authenticated `sbpd` data routes accept only RS256 Amp workload-identity JWTs
from issuer `https://ampcode.com/api/workload-identity` for exact audience
`sbpd`. The server requires an immutable project-ID allowlist and an exact
`owner/repo` project alias before authenticated startup. It verifies signature,
issuer, audience, expiry, issued-at time, maximum one-hour lifetime,
`email_verified=true`, `token_use=exchanged`, and nonempty `email`, `jti`,
`project_id`, `sub`, `thread_id`, and `user_id`. The labeled subject must equal
the project/user/thread claims. Optional user or workspace allowlists can
narrow authority further.

A live token minted in this project Orb on 2026-08-06 contained:

```text
aud email email_verified exp iat iss jti project_id sub thread_id token_use user_id
```

The token was inspected in memory and not retained. It omitted
`workspace_id`, so Skillbox does not require that claim unless a future real
token and relying-party contract prove it. This observation proves the Amp
mint and claim shape only; it does not prove that Sweet Potato/SPAPS accepts
the identity.

For non-loopback reads, `scripts/lib/sbp_client.py` mints a short-lived token
using `amp orb id-token` by default, keeps it only in process memory, and
retries once with a newly minted token after HTTP 401. Runtime authentication
has no environment-token, file-token, or static-secret shortcut: every online
authenticated request must mint Amp OIDC. Tests may inject an in-memory minter
callable, but production dispatch does not expose that injection surface.
The client rejects minted tokens with the wrong issuer, claims, subject,
token-use, algorithm, audience, lifetime, or optional workspace binding.
`sbpd` always performs RS256 signature and immutable allowlist verification;
there is no shared-secret authentication path.

## Authenticated skill source and cold resume

The first authenticated `skill pull` requires working auth and transport. It
fails with a typed error when either is unavailable; it does not substitute a
local skill, another project, a campaign, or a family source.

After server and bundle verification, the client stages only the exact capsule
bytes in an Orb-local private cache outside `.agents/skills`, `.claude/skills`,
and `.codex/skills`. The mode-`0600` lock binds:

- project alias and immutable project UUID;
- exact 40-character remote Git commit;
- capsule, verified skill-tree, and lock hashes;
- thread, user, and stable resume identities;
- short-lived lease/JTI and server resolution-receipt IDs; and
- `private` visibility.

The server identity headers must match the minted token before bytes are
cached. Offline resume requires all bindings and private modes to match, then
re-unpacks and verifies the exact tree before returning `SKILL.md`. Tampering,
missing identity, symlinks, discovery-root placement, or absent cache fails
typed and closed. The cache is an accelerator, not a discoverable or durable
skill source.

## Sweet Potato/SPAPS read boundary

The only currently accepted Skillbox-owned remote surface is the fixed GET-only
`sbpd` service/API boundary:

- `GET /healthz` for service health;
- authenticated `GET /v1/orb-kit` for the deterministic bootstrap client;
- `GET /v1/cass/status`;
- `GET /v1/cass/search?q=<one nonempty query>`; and
- `GET /v1/skill/pull/<one validated skill name>`.

There is no arbitrary command or path delegation. POST, PUT, PATCH, and DELETE
return 405 without calling Cass or skill code. Binding is limited to loopback
or a literal Tailnet address; public/wildcard binds are forbidden.

Hosted SPAPS is Sweet Potato-owned and is a distinct relying party. The
project capability declaration recognizes only the *presence* of the exact
configured `SPAPS_REMOTE_READ_URL`; it does not call it during readiness or
claim authorization. Before a SPAPS route can be accepted, Sweet Potato must
supply a reviewed verifier contract and a live, sanitized receipt binding the
exact project identity, audience, destination, allowed GET service/API reads,
and negative infrastructure/admin/mutation probes. Until then:

- no local `spaps local` stack may stand in for hosted SPAPS;
- no guessed audience, endpoint, claim, host alias, or workspace requirement
  may be introduced;
- publishable, user, and admin credentials must not be conflated; and
- infrastructure/admin and all SPAPS mutations remain forbidden.

See [the SPAPS auth boundary](../spaps-cli-auth-boundary.md) and
[Orb Tailnet bootstrap](../orb-tailnet-bootstrap.md).

## Application deploy overlay

[`workspace/project-orb-deploy.json`](../../workspace/project-orb-deploy.json)
is the Skillbox-owned overlay for the existing deployment lifecycle:

```text
client projection
  → client-publish --deploy-artifact
  → versioned deploy.json + exact archive/tree hashes
  → scripts/box.py upgrade --deploy-manifest ...
```

The overlay does not create a second deploy engine. Its preflight validator
reuses `scripts/box.py` manifest parsing and requires schema version 1, an
exact 40-character source commit, the client ID, payload tree SHA-256, archive
SHA-256, an exact previous manifest for rollback, and credential *names* only.
The private receipt store is ignored and mode `0600`. Health is a planned
`box.py status` read. Dry-run and rollback remain non-mutating plans.

```bash
python3 scripts/orb/deploy_preflight.py \
  --box-id '<client-id>' \
  --deploy-manifest '<exact-deploy.json>' \
  --previous-deploy-manifest '<previous-exact-deploy.json>' \
  --output .skillbox-state/project-orb/deploy-receipts/preflight.json
```

Production apply is always `forbidden` in the local preflight. The existing
operator MCP preview/approval gate, destination health proof, authoritative
receipt, and rollback remain external requirements. Ordinary Orb credentials
cannot provision/destroy boxes, administer infrastructure/DNS/Tailnet, rotate
credentials, or apply production. A local fixture can prove the overlay and
negative authority contract, but never production deployment acceptance.

## Acceptance and typed external stops

Independent review blocks the root production-readiness epic. Local tests may
accept project skill projection and offline readiness. The following stops
remain external and cannot be closed with fixtures:

| Stop code | Required evidence |
|---|---|
| `LIVE_RP_AUTH_UNPROVEN` | Real relying-party receipt accepting the exact allowed project token and rejecting wrong project/claims |
| `SPAPS_VERIFIER_RECEIPT_MISSING` | Sweet Potato-owned verifier contract plus live read/deny receipt from this authorized project Orb |
| `PRODUCTION_APPLY_AUTHORITY_MISSING` | Explicit operator grant, exact production apply/health receipt, and proven rollback artifact |
| `INDEPENDENT_ACCEPTANCE_PENDING` | Review of implementation, receipts, negative tests, Bead graph, and remaining external stops |

No raw token, credential, private endpoint, or unredacted verifier receipt may
be put in Git or Beads. Local fixtures are implementation proof only.
