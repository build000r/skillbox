# SPAPS CLI Auth Boundary

Protocol/boundary note for the SPAPS-backed CLI auth and token-renewal route.
Produced by a `saas-cli-auth-flow` boundary analysis (2026-07-11) for bead
`skillbox-no-ragrets-saas-cli-auth-flow-skill-pass-3soq`, recorded against
`skillbox-spaps-token-refresh-iuk`.

This document decides **which repo owns each piece** of the CLI-to-web auth
surface. Skillbox does not implement an auth system; it consumes the SPAPS CLI
(`spaps`, npm package built from the `sweet-potato` repo) and wires it into
loopback-only runtime services. All statements below were verified against
`spaps` 0.9.3 CLI source and the skillbox runtime wiring, with no live auth
endpoint calls.

## The two SPAPS auth surfaces skillbox touches

1. **Local dev auth runtime** — the
   [`git-repo-http-service-bootstrap-spaps-auth`](../workspace/client-blueprints/git-repo-http-service-bootstrap-spaps-auth.yaml)
   blueprint runs `spaps local` on `127.0.0.1:3301` and materializes repo-local
   RBAC/auth fixtures for client apps. This is a loopback service used for
   local RBAC testing; it never holds production credentials.
2. **Hosted SPAPS credentials** — agents and the operator-booking surface talk
   to a hosted SPAPS server using either stored CLI credentials
   (`spaps login` / `spaps token`) or env-provided keys
   (`NEXT_PUBLIC_SPAPS_PUBLISHABLE_KEY` as `X-API-Key`,
   `SPAPS_AUTH_ACCESS_TOKEN` as bearer).

## Ownership boundary table

| Auth-flow item | Owner | Status / notes |
|---|---|---|
| Browser PKCE login (Tier 1) | SPAPS (`sweet-potato`) | Server routes exist (`/auth/cli-login`, `/auth/callback`, `/auth/token` scaffolding); not used by skillbox agents, which are headless. Out of scope for skillbox. |
| Manual/headless copy-paste PKCE (Tier 2) | Out of scope | Superseded by the device-code path for every skillbox use case. |
| RFC 8628 device-code login (Tier 3) | SPAPS (`sweet-potato`) | Implemented: `spaps login` runs the device flow with `slow_down`/interval backoff and stores a public-client credential (`client_id`) per server URL. Skillbox consumes it as-is. |
| Token refresh | SPAPS (`sweet-potato`) | Implemented in `spaps` >= 0.9.x: `spaps token` silently refreshes within 30s of expiry; authenticated calls do a one-shot refresh on 401 via `POST /auth/refresh` with `refresh_token` + `client_id` (public client, no app API key), under a credentials-file lock with rotation-race retry. This removes the original blocker on `skillbox-spaps-token-refresh-iuk`. |
| Token revoke / logout | SPAPS (`sweet-potato`) | `spaps logout` does a best-effort server-side revoke (`/auth/logout` with the refresh token) and always clears local credentials. |
| Secure credential storage | SPAPS (`sweet-potato`) | Credentials file written atomically with `0600` mode inside a `0700` dir (`~/.config/spaps/`). No OS-keyring tier yet — SPAPS-owned gap, acceptable for single-user boxes. |
| CI / machine fallback | SPAPS + skillbox split | SPAPS: `SPAPS_ACCESS_TOKEN` env var bypasses the credentials file and opts out of refresh. Skillbox: operator booking resolves `NEXT_PUBLIC_SPAPS_PUBLISHABLE_KEY` (API key) and `SPAPS_AUTH_ACCESS_TOKEN` (bearer) from managed env files; env var names are overlay-configurable. |
| Redaction / logging rules | Skillbox | `runtime_manager/operator_booking.py` strips `api_key`, `access_token`, and `_`-prefixed keys from any surfaced config and reports only `*_configured` booleans; projection/context rendering never emits `SKILLBOX_SWIMMERS_AUTH_TOKEN`; credentialed booking requests refuse non-local plain-HTTP URLs. |
| Local auth service posture | Skillbox | Blueprint defaults keep `spaps local`, fixtures, CORS origins, and healthchecks on `127.0.0.1`/`localhost`. The only off-loopback path is the first-box tailnet augmentation in `scripts/box.py`, which rewrites the browser-visible URLs to the box's private Tailscale IP — never a public interface. |
| Swimmers token exposure | Skillbox | Publishing swimmers beyond loopback requires the explicit `SKILLBOX_SWIMMERS_EXPOSE=1` opt-in plus token auth mode; the contract payload derivation rejects exposure without the opt-in. |
| Keepwarm cron (buildooor proxy refresh) | Deprecated (box ops) | The `spaps-keepwarm` prototype refreshed through the buildooor.com proxy because older CLIs could not refresh headlessly. With public-client refresh in `spaps` >= 0.9.x it is redundant and, because SPAPS refresh tokens are single-use/rotating, running it alongside CLI refresh risks rotation races. Retire it once production refresh is verified. |
| `spaps` version pin in the workspace image | Skillbox | `Dockerfile` pins `spaps@0.7.7`, which predates `login`/`token`/refresh. All blueprint-used flags (`local --port/--runtime-dir/--runtime-source`, `fixtures apply --dir/--port/--base-url`) still exist in 0.9.3 (published on npm). Bump is a follow-up skillbox change gated on an image rebuild test. |

## Skillbox invariants (must hold after any change)

- No SPAPS auth surface listens beyond loopback or the private tailnet
  boundary. Blueprint defaults are loopback; tailnet URLs are opt-in and
  derived from the box's Tailscale address.
- Secrets never appear in rendered context, projections, JSON status, or
  docs. Only `*_configured` booleans and env-var *names* are surfaced.
- Skillbox never stores SPAPS tokens itself; the credentials file is owned and
  rotated by the `spaps` CLI.
- Skillbox never hand-rolls refresh (no curl loops against `/auth/refresh`);
  headless renewal is delegated to `spaps token` / the CLI's 401-refresh path.

## What skillbox tests prove

- `tests/test_spaps_auth_boundary.py` — blueprint auth defaults are
  loopback-only, no secret-like defaults ship in the blueprint, and this
  document keeps its required boundary sections without embedding token-like
  strings.
- `tests/test_operator_booking.py` — publishable key goes out as `X-API-Key`,
  bearer token comes from the configured env var, dry-run works without
  credentials, and credentialed requests reject non-local HTTP URLs.
- `tests/test_box_lifecycle.py`, `tests/test_box.py`,
  `tests/test_swimmers_script.py` — swimmers token exposure requires explicit
  opt-in plus token auth.
- `tests/test_client_projection_units.py`, `tests/test_runtime_manager.py` —
  auth token values never appear in rendered projections/context.
- `tests/test_operator_mcp_server.py` — the default first-box blueprint is the
  SPAPS-auth blueprint.

## Delegated to SPAPS (`sweet-potato` repo)

- Device-flow login, token mint/refresh/revoke, public-client policy and
  refresh-token TTLs, credential storage hardening (keyring tier), and any
  future `spaps token --refresh` explicit flag.
- Verification that the production server accepts public-client refresh
  (`client_id` + `refresh_token`, no app API key) so the deprecated keepwarm
  path can be removed.
