Internal runtime manager for `skillbox`.

This is the inside-the-box control surface. It reads `workspace/runtime.yaml`
and manages the declared runtime graph for repos, artifacts, env files,
installed skills, services, logs, and sanity checks that live inside the
workspace.

Useful commands:

```bash
python3 .env-manager/manage.py render
python3 .env-manager/manage.py sync
python3 .env-manager/manage.py doctor
python3 .env-manager/manage.py status
python3 .env-manager/manage.py up --profile surfaces
python3 .env-manager/manage.py down --profile surfaces --service api-stub
python3 .env-manager/manage.py logs --profile surfaces --service api-stub --lines 80
python3 .env-manager/manage.py client-init --list-blueprints
python3 .env-manager/manage.py first-box personal --format json
python3 .env-manager/manage.py private-init --path ../skillbox-config --format json
python3 .env-manager/manage.py client-init acme-studio --blueprint git-repo-http-service-bootstrap --set PRIMARY_REPO_URL=https://github.com/acme/app.git --set BOOTSTRAP_COMMAND='pnpm install && mkdir -p .skillbox && touch .skillbox/bootstrap.ok' --set SERVICE_COMMAND='pnpm dev'
python3 .env-manager/manage.py client-project personal
python3 .env-manager/manage.py client-project personal --profile surfaces --output-dir ./builds/clients/personal-surfaces
python3 .env-manager/manage.py client-diff personal --profile surfaces
python3 .env-manager/manage.py client-publish personal --acceptance --commit --profile surfaces
python3 .env-manager/manage.py sync --client personal
python3 .env-manager/manage.py render --client personal --profile surfaces
python3 .env-manager/manage.py status --profile swimmers
```

`sync` reconciles repos, env files, and managed artifacts against their
declared pins or source files, creates artifact/log directories, and installs
skills from declared `skill-repos.yaml` configs (clone/fetch repos, filtered
copy into install targets), writing lockfiles for each selected skill set.

`up`, `down`, `restart`, and `logs` are the first lifecycle commands for
declared services. `up` runs `sync` first, then starts manageable services and
waits for their declared healthchecks when present.

The mental model is:

- `core` is always active
- `--client` activates a client overlay such as `personal` or `vibe-coding-client`
- `--profile` activates optional non-client overlays such as `surfaces`
- `--profile connectors` is the runtime connector surface: pinned binaries and MCP services
- `--profile connectors-dev` adds optional FWC/DCG source checkouts for inspection or development
- `client-init` scaffolds `${SKILLBOX_CLIENTS_HOST_ROOT:-./workspace/clients}/<client>/overlay.yaml`
  plus the companion skill directories, `skill-repos.yaml`, and planning roots for a new overlay
- `client-init --blueprint ...` appends reusable repos, services, logs, and
  checks to that scaffold so `render`, `sync`, and `up` immediately work on a
  concrete client shape; the blessed hardened-v1 path is `git-repo-http-service-bootstrap`
- `first-box <client>` is the canonical first-run path: it runs `private-init`,
  reuses or scaffolds the selected client, proves readiness with `acceptance`,
  and writes `sand/<client>/` via `client-open`
- `SKILLBOX_FWC_CONNECTORS` is the box-level connector superset; `client.connectors`
  in overlays can only narrow that set, and `doctor` / `acceptance` fail early
  when a client widens it
- `client-project <client>` compiles a client-safe bundle under
  `builds/clients/<client>/` with a single-client `workspace/runtime.yaml`,
  only that client's overlay/skill files, and a sanitized `runtime-model.json`
  plus `projection.json`
- `client-open <client> --from-bundle <dir>` re-opens a reviewed projection
  bundle into `sand/<client>/` without running live `focus`
- `client-diff <client> --target-dir <repo>` is the review step before
  promotion, showing file-level and runtime-surface deltas against the current
  published payload
- `client-publish <client> --target-dir <repo>` promotes the reviewed bundle
  into `clients/<client>/current/` plus `publish.json`, and `--acceptance`
  also persists compact readiness evidence into `clients/<client>/acceptance.json`

Client overlays usually point at repo roots under `/monoserver`, which is the
host parent directory mounted into the workspace container.

The outer repo-level `.env` still controls Docker and top-level workspace
settings. This internal manager is for the contents of the box, not for the
host/container launch boundary.
