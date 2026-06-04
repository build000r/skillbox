# Tailnet App Addressing

This note records the current browser addressing contract for local-runtime web
services that need to be reachable from the operator's Tailnet.

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

A web app is phone-viewable when its runtime service and app config agree on
the same direct Tailnet URL:

- reserve one stable port for the app
- start the app with that port and a Tailnet-only bind address
- configure browser-visible base URLs, auth callbacks, websocket URLs, API
  origins, and CORS allowlists with `http://<tailnet-host>:<app-port>`
- point the service healthcheck at a listener the runtime can actually reach
- include the app in `local-frontend` or `local-all` when it should be part of
  the local browser surface

Loopback services remain valid for same-box automation, but `127.0.0.1` and
`localhost` are not phone-viewable Tailnet URLs.

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

## Deferred Ingress Models

The older path-prefix ingress lane is deferred for browser-facing apps. It
would use a single private listener, for example:

```text
http://<tailnet-host>:9080/<app-prefix>
```

That model is not the default because the current proxy forwards the original
request path to the upstream. Apps with absolute assets, routers, auth
callbacks, or websocket paths must then be rebuilt for a base path such as
`/<app-prefix>/`, and a quick proxy route does not make them phone-viewable.

Subdomain-per-app addressing is also deferred:

```text
http://<app>.<tailnet-host>:9080/
```

That model needs host-header routing plus a Tailnet DNS or Tailscale Serve
strategy for per-app names. Those are useful future options, but they are not
required for the current local browser surface.

## Follow-On Work

The next implementation slices should build on the port-per-app contract:

- reserve and document app ports in the client overlays
- bind browser-facing app servers to the Tailnet address instead of public
  wildcard interfaces
- update healthchecks, auth, CORS, and generated browser config to use each
  app's direct Tailnet URL
- keep the path-prefix proxy metadata as compatibility plumbing, not the
  current phone-viewable app contract
