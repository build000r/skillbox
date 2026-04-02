# Skillbox → Real Dev → Prod Roadmap

How skillbox transitions from agent/skill development runtime to a production-grade devbox layer that promotes cleanly into the sweet-potato nginx/deploy stack.

---

## Phase 0: Already Here (Now)

Skillbox as a dev runtime is production-grade. The runtime graph, client overlays, service lifecycle, health checks, installer, pulse daemon — all of this works. It's being used for real dev right now: building skills, running agents, managing clients.

What's missing isn't the box — it's the **bridge to sweet-potato's nginx/deploy layer** so the box can serve as the staging/devbox tier of a real deploy pipeline.

---

## Phase 1: Skillbox-as-Devbox

**Goal:** Skillbox becomes the canonical place where you develop *and preview* services before they hit the sweet-potato prod stack.

### What needs to happen

- **Wire the reverse proxy stub.** The `docker-compose.yml` already has `api` and `web` stub services. Replace those stubs with actual service containers (or proxy-pass to local processes). This gives `localhost:3001/3002` preview URLs inside the box.
- **Client overlay for sweet-potato.** The `happy-trail` sand client already references `${SKILLBOX_MONOSERVER_ROOT}/sweet-potato`. Make a real client overlay that mounts sweet-potato's dev containers (`spaps-python`, `spaps-python-db`, `spaps-python-redis`) as skillbox services with health checks in `runtime.yaml`.
- **Shared network bridge.** Sweet-potato's prod compose uses `reverse-proxy` and `spaps-python-internal` networks. The devbox needs a parallel `skillbox-dev` network that the sweet-potato dev containers join, so box services can talk to SPAPS auth/billing locally.

### Gate

A `skillbox render && skillbox up` brings up sweet-potato's dev stack alongside other services, with health checks passing.

---

## Phase 2: Dev → Staging Parity

**Goal:** What runs in the box matches what runs in prod, minus the domain/SSL.

- **Mirror sweet-potato's nginx config locally.** Take `deploy/reverse-proxy/nginx.conf` and create a `skillbox-dev` variant — same upstream blocks, same rate-limit zones, but pointing at local containers instead of prod. This catches routing bugs before they hit the droplet.
- **Env parity.** Sweet-potato already has `local.env` / `override.env` / `prod.env`. Add a `skillbox.env` profile that the runtime manager renders from `runtime.yaml` declarations, so you get the same env var shape in both environments.
- **Deploy skill integration.** The `deploy` skill already knows about modes, hosts, and health endpoints. Add a `skillbox-local` mode that targets the box's containers instead of SSH'ing to the droplet. Same skill, different target.

### Gate

Local nginx routing matches the prod shape. Env vars render identically between box and droplet.

---

## Phase 3: Clean Prod Transition

**Goal:** The `deploy` skill promotes from skillbox-dev to sweet-potato prod with one command.

- **GHCR image pipeline.** Sweet-potato already deploys via GHCR images. Skillbox services that are ready for prod get the same treatment — Dockerfile → GHCR → `deploy.sh` pulls on the droplet.
- **Nginx site-available templating.** Sweet-potato's `sites-available/` already handles multiple apps (`sweet_potato.conf`, htma, ingredient, etc.). New services developed in skillbox get their own `.conf` generated from the runtime graph — same pattern, just a new upstream block.
- **Blue-green via the box.** Sweet-potato already has `blue-green-deploy.sh`. The devbox becomes the "blue" environment for smoke testing before the droplet's "green" gets swapped.
- **Tailscale as the glue.** Both systems already use Tailscale. The box and the droplet are on the same tailnet. The `ssh-info` skill already knows the topology. Promotion is just "build here, push image, pull there."

### Gate

One deploy command promotes a service from the box to the droplet with no manual nginx or compose editing.

---

## Architecture

```
┌─────────────────────────────────────────────┐
│  Skillbox (your machine / DO devbox)        │
│  ┌─────────────┐  ┌──────────────────────┐  │
│  │ runtime.yaml│→ │ dev containers       │  │
│  │ + overlays  │  │ (spaps-dev, your svc)│  │
│  └─────────────┘  └──────────┬───────────┘  │
│                              │ tailnet      │
│  ┌───────────────────────────▼───────────┐  │
│  │ skillbox-dev nginx (mirrors prod)     │  │
│  └───────────────────────────────────────┘  │
└──────────────────────┬──────────────────────┘
                       │ promote (GHCR image push)
┌──────────────────────▼──────────────────────┐
│  Sweet-Potato Droplet (prod)                │
│  ┌───────────────────────────────────────┐  │
│  │ nginx reverse-proxy (SSL, rate-limit) │  │
│  │ → spaps-python, htma, your new svc   │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

---

## Timeline

| Phase | Milestone | Gate |
|-------|-----------|------|
| **0 (Now)** | Skillbox works for skill/agent dev | Already passing |
| **1** | Sweet-potato dev containers in runtime.yaml | `skillbox up` runs SPAPS locally |
| **2** | Nginx parity + env parity | Local routing matches prod shape |
| **3** | Deploy skill promotes box → droplet | One command, same skill, different target |

---

## Key Insight

No new infra needs to be built. Sweet-potato already has the prod nginx + deploy scripts. Skillbox already has the runtime graph + service lifecycle. Phase 1 is wiring them together through a client overlay and a shared Docker network. Everything after that is parity and polish.
