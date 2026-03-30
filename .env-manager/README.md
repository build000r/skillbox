Internal runtime manager for `skillbox`.

This is the inside-the-box control surface. It reads `workspace/runtime.yaml`
and manages the declared runtime graph for repos, installed skills, services,
logs, and sanity checks that live inside the workspace.

Useful commands:

```bash
python3 .env-manager/manage.py render
python3 .env-manager/manage.py sync
python3 .env-manager/manage.py doctor
python3 .env-manager/manage.py status
```

`sync` now reconciles repo/log directories and installs the declared packaged
default skills into the managed Claude and Codex homes, writing a generated
lockfile at `workspace/default-skills.lock.json`.

The outer repo-level `.env` still controls Docker and top-level workspace
settings. This internal manager is for the contents of the box, not for the
host/container launch boundary.
