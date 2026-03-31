Internal runtime manager for `skillbox`.

This is the inside-the-box control surface. It reads `workspace/runtime.yaml`
and manages the declared runtime graph for repos, artifacts, installed skills,
services, logs, and sanity checks that live inside the workspace.

Useful commands:

```bash
python3 .env-manager/manage.py render
python3 .env-manager/manage.py sync
python3 .env-manager/manage.py doctor
python3 .env-manager/manage.py status
python3 .env-manager/manage.py up --profile surfaces
python3 .env-manager/manage.py down --profile surfaces --service api-stub
python3 .env-manager/manage.py logs --profile surfaces --service api-stub --lines 80
python3 .env-manager/manage.py client-init acme-studio
python3 .env-manager/manage.py sync --client personal
python3 .env-manager/manage.py render --client personal --profile surfaces
python3 .env-manager/manage.py status --profile swimmers
```

`sync` reconciles repo/log directories and installs the declared packaged skill
sets for the active scope, writing generated lockfiles for each selected skill
set.

`up`, `down`, `restart`, and `logs` are the first lifecycle commands for
declared services. `up` runs `sync` first, then starts manageable services and
waits for their declared healthchecks when present.

The mental model is:

- `core` is always active
- `--client` activates a client overlay such as `personal` or `vibe-coding-client`
- `--profile` activates optional non-client overlays such as `surfaces`
- `client-init` scaffolds `workspace/clients/<client>/overlay.yaml` and the
  companion skill directories for a new overlay

Client overlays usually point at repo roots under `/monoserver`, which is the
host parent directory mounted into the workspace container.

The outer repo-level `.env` still controls Docker and top-level workspace
settings. This internal manager is for the contents of the box, not for the
host/container launch boundary.
