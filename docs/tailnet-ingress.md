# Tailnet App Addressing

This note defines the public runtime contract for browser-facing services that
should be reachable from a Tailnet device. Operator-specific app names, ports,
and rollout evidence belong in private client overlays or private runbooks.

## Decision

Use one Tailnet-bound port per browser-facing app:

```text
http://<tailnet-host>:<app-port>/
```

`<tailnet-host>` should be the box's MagicDNS name when available, and can fall
back to the box's Tailnet IP. Each app gets a stable, non-overlapping port. The
app's dev server should bind to the Tailnet address for remote browser access
instead of binding to every interface on hosts that also have a public address.

## App Contract

A web app is viewable from another Tailnet device when its runtime service and
app config agree on the same direct Tailnet URL:

- reserve one stable port for the app
- start the app with that port and a Tailnet-only bind address
- configure browser-visible base URLs, auth callbacks, websocket URLs, API
  origins, and CORS allowlists with `http://<tailnet-host>:<app-port>`
- point the service healthcheck at a listener the runtime can actually reach
- include the app in the profile that should expose local browser surfaces

Loopback services remain valid for same-box automation, but `127.0.0.1` and
`localhost` are not viewable from a phone or laptop elsewhere on the Tailnet.

Scene-style multi-tier apps are an exception to the direct-port contract when
their browser bundles point at loopback-only API/auth services. Keep those as
local-box workflows until a dedicated proxy or backend/auth refactor makes the
whole dependency graph Tailnet-capable.

## Verification

Start from the active runtime profile:

```sh
python3 .env-manager/manage.py status --profile <profile> --format json --compact
python3 scripts/tailnet_app_smoke.py --client <client>
```

For a real second-device check, open each declared
`http://<tailnet-host>:<app-port>/` URL from a phone or laptop already joined to
the Tailnet. Runtime status should show the direct URL on the service row.

Use JSON when automation needs to read the same contract. The app service row
should include `endpoint_url`, `exposure: "tailnet-direct"`, and
`viewable_from_tailnet: true`. Services that bind `0.0.0.0` or `::` are reported
as `exposure: "wildcard-direct"` when a Tailnet host can be substituted. Treat
that as a warning state, not as a passing Tailnet-only app port: the service is
addressable through the Tailnet host but is also bound to every host interface.

## Rollout And Rollback

Use this sequence when enabling or changing a selected direct-port app:

1. Reserve a stable port that does not overlap any existing app in the selected
   profile.
2. Update the client overlay command, `origin_url`, healthcheck, and browser
   config so they all use `http://<tailnet-host>:<app-port>/`.
3. Remove obsolete private-root `ingress_routes` for the app when direct-port
   access is the intended browser surface.
4. Confirm the resolved model before starting or restarting services:

   ```sh
   python3 .env-manager/manage.py render --format json --profile <profile> --client <client>
   python3 .env-manager/manage.py up --dry-run --format json --profile <profile> --client <client>
   ```

5. Start or resume the runtime and verify status:

   ```sh
   python3 .env-manager/manage.py status --client <client> --profile <profile> --compact
   python3 scripts/tailnet_app_smoke.py --client <client>
   ```

When the box is under high or critical disk pressure, prefer `up --dry-run`,
status, and smoke checks over forced cold restarts. Cold frontend builds can
consume enough temporary storage to make validation less representative.

Rollback is a config revert, not a runtime trick:

1. Revert the overlay commit or change the app command, `origin_url`, and
   healthcheck back to the previous loopback values.
2. Restore any previous ingress route only if the rollback is intentionally
   returning to a single-client or path-prefix proxy workflow.
3. Re-run `render`, `up --dry-run`, and `status` for the affected client.
4. Restart only the affected service, or restart pulse if the active profile
   needs to re-adopt the service list.

## Overlay Shape

Client overlays should make the app port and browser URL explicit. The exact
command differs by framework, but the shape is:

```yaml
client:
  services:
    - id: example-web
      kind: http
      command: npm run dev -- --host ${EXAMPLE_TAILNET_BIND_HOST} --port ${EXAMPLE_WEB_PORT}
      healthcheck:
        type: http
        url: http://${EXAMPLE_TAILNET_BIND_HOST}:${EXAMPLE_WEB_PORT}/
      profiles:
        - local-frontend
        - local-all
```

For apps with local auth, fixture generation, or CORS bootstrapping, use the
same browser URL in those environment values. A service that only reports a
loopback healthcheck may still start, but status should not claim it is
Tailnet-viewable unless its command or metadata exposes a direct Tailnet URL.
Likewise, a wildcard bind can be useful while debugging a local-only app, but
it remains `wildcard-direct` until the runtime command binds to a Tailnet-only
host.

## Deferred Ingress Models

The older path-prefix ingress lane is deferred for browser-facing apps. It
would use a single private listener, for example:

```text
http://<tailnet-host>:9080/<app-prefix>
```

That model is not the default because the current proxy forwards the original
request path to the upstream. Apps with absolute assets, routers, auth
callbacks, or websocket paths must then be rebuilt for a base path such as
`/<app-prefix>/`, and a quick proxy route does not make them device-viewable.
