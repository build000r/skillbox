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
python3 .env-manager/manage.py client-init acme-studio
python3 .env-manager/manage.py client-init acme-studio --blueprint git-repo --set PRIMARY_REPO_URL=https://github.com/acme/app.git
python3 .env-manager/manage.py client-init acme-studio --blueprint git-repo-http-service --set PRIMARY_REPO_URL=https://github.com/acme/app.git --set SERVICE_COMMAND='pnpm dev'
python3 .env-manager/manage.py client-project personal
python3 .env-manager/manage.py client-project personal --profile surfaces --output-dir ./builds/clients/personal-surfaces
python3 .env-manager/manage.py client-diff personal --target-dir ../skillbox-config-control --profile surfaces
python3 .env-manager/manage.py client-publish personal --target-dir ../skillbox-config-control --commit --profile surfaces
python3 .env-manager/manage.py sync --client personal
python3 .env-manager/manage.py render --client personal --profile surfaces
python3 .env-manager/manage.py status --profile swimmers
```

`sync` reconciles repos, env files, artifact/log directories, and the declared
packaged skill sets for the active scope, writing generated lockfiles for each
selected skill set.

`up`, `down`, `restart`, and `logs` are the first lifecycle commands for
declared services. `up` runs `sync` first, then starts manageable services and
waits for their declared healthchecks when present.

The mental model is:

- `core` is always active
- `--client` activates a client overlay such as `personal` or `vibe-coding-client`
- `--profile` activates optional non-client overlays such as `surfaces`
- `client-init` scaffolds `${SKILLBOX_CLIENTS_HOST_ROOT:-./workspace/clients}/<client>/overlay.yaml`
  and the companion skill directories for a new overlay
- `client-init --blueprint ...` appends reusable repos, services, logs, and
  checks to that scaffold so `render`, `sync`, and `up` immediately work on a
  concrete client shape
- `client-project <client>` compiles a client-safe bundle under
  `builds/clients/<client>/` with a single-client `workspace/runtime.yaml`,
  only that client's overlay/skill files, and a sanitized `runtime-model.json`
  plus `projection.json`
- `client-diff <client> --target-dir <repo>` is the review step before
  promotion, showing file-level and runtime-surface deltas against the current
  published payload
- `client-publish <client> --target-dir <repo>` promotes the reviewed bundle
  into `clients/<client>/current/` plus `publish.json`

Client overlays usually point at repo roots under `/monoserver`, which is the
host parent directory mounted into the workspace container.

The outer repo-level `.env` still controls Docker and top-level workspace
settings. This internal manager is for the contents of the box, not for the
host/container launch boundary.
