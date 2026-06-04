# Tailnet Ingress Addressing

This note records the addressing decision for local-runtime web services that
need to be reachable from a browser on the operator's tailnet.

## Decision

Use path-prefix-per-app addressing on the single private ingress listener:

```text
http://<tailnet-host>:9080/<app-prefix>
```

The private listener is the one-port browser surface for managed local-runtime
apps. Each app gets one stable path prefix on that listener. The runtime route
manifest continues to describe routes with `listener`, `path`, `match`, and
`service_id`; future registry work can assign those paths from a dedicated
`path_prefix` field, but the addressing model remains path based.

## Rationale

The operator requirement is that every managed app is reachable on the same
port. Path prefixes satisfy that with the ingress proxy that exists today:

- `scripts/ingress_proxy.py` dispatches by request path and listener.
- `workspace/runtime.yaml` already declares one `ingress-router` service for
  path-based ingress.
- `.env-manager/manage.py up` renders the active `ingress_routes` manifest
  before starting services, so status and route files describe the same graph.
- A single Tailscale node has one node identity and one MagicDNS name; app
  subdomains would require host routing plus extra DNS or Serve setup that is
  not part of the current runtime contract.

## Route Shape

A client overlay exposes a web service by declaring an ingress route:

```yaml
ingress_routes:
  - id: example-web-private
    service_id: example-web
    listener: private
    path: /example
    match: prefix
    profiles:
      - local-frontend
```

The matched service must have an `origin_url`, for example
`http://127.0.0.1:5173`. The proxy matches `/example` and `/example/...` when
`match: prefix` is used. It forwards the original request path to the upstream;
it does not strip `/example`.

## App Contract

Because the proxy does not rewrite paths, each app must be able to serve under
its assigned prefix. Frontend apps normally need:

- a base URL or base href set to `/<app-prefix>/`
- asset URLs that resolve under the prefix instead of absolute `/assets/...`
- router basename or equivalent client-side routing prefix
- auth callback, API, websocket, and CORS origins that use the prefixed
  browser URL when those features are present

If an app cannot serve under a prefix yet, do not treat the route as
phone-viewable. Either fix the app's base-path settings or wait for a later
optional `strip_prefix` proxy feature.

## Deferred Alternative: Subdomains

Subdomain-per-app addressing is intentionally deferred. It would look like:

```text
http://<app>.<tailnet-host>:9080/
```

That model needs more than a route registry. The runtime would need
host-header routing in the proxy, a per-app host assignment contract, and a
Tailnet DNS or Tailscale Serve strategy that reliably resolves those names.
Those pieces are useful future options, but they are outside the current
aggregate-ingress slice.

## Follow-On Work

The next implementation slices should build on this decision:

- assign non-overlapping prefixes for every managed web app
- strengthen validation for prefix collisions and ambiguous routes
- teach app overlays to serve assets and routers under their assigned prefix
- optionally add `strip_prefix` only for apps that cannot be base-pathed
- keep subdomain support as a later host-routing feature, not a prerequisite
