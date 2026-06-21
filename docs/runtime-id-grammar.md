# Runtime ID grammar (canonical)

Single source of truth for the slug grammar used by runtime identifiers. Every
`service` / `task` / `client` / `profile` / `repo` / `artifact` / `skill` /
`check` id is joined into a filesystem path somewhere (`logs/<service-id>/`,
`.skillbox-state/<client>/`, pid/lock names, overlay directories, skill install
targets). A malformed id like `a/b` reshapes that directory tree and `../x`
escapes the intended root, so ids are validated as strict slugs **before** any
code joins them into a path.

## The grammar

```
^[a-z0-9][a-z0-9_-]{0,63}$
```

- lowercase ASCII letters and digits, plus `_` and `-` after the first
  character;
- must start with an alphanumeric character;
- 1–64 characters total;
- **no** `/` or `\` (path separators), **no** `.` (so `..` can never appear),
  **no** leading `-` (so an id can never be mistaken for a CLI flag), **no**
  uppercase, **no** whitespace, **no** empty string.

Violations are **rejected loudly**, never sanitized: silently rewriting an id
would desync the runtime model, the brain graph, and the on-disk layout from
each other.

## Where it is enforced

- **Model load** — `scripts/lib/runtime_model.py` (`RUNTIME_ID_PATTERN`,
  `validate_runtime_id`, `validate_runtime_model_ids`). `build_runtime_model`
  validates every id of every enforced kind and raises
  `RuntimeIdValidationError` (stable code `RUNTIME_ID_INVALID`) with
  `context={"id", "kind", "source_file"}` provenance and a rename playbook in
  `next_actions`. The runtime_manager CLI promotes this to a typed
  `ValidationError(RUNTIME_ID_INVALID, ...)` so the surfaced JSON envelope
  carries the code + provenance.
- **Client creation** — `runtime_manager/shared.py` `validate_client_id`
  (used by `client-init` / `onboard` via `scaffold_client_overlay`) decides
  accept/reject with this same `RUNTIME_ID_PATTERN`, so a slug accepted at
  creation time is exactly a slug the model will later accept. (It surfaces the
  established `invalid_client_id` recovery code for the create-time front door.)

## Box ids are a separate, aligned surface

`scripts/box.py` `_validate_box_id` / `_validate_profile_name` validate **box**
ids and box-profile names (DigitalOcean/Tailscale lifecycle), which are NOT
runtime ids. They intentionally allow a slightly wider class
(`[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}` — additionally permitting `.` and
uppercase) because box ids historically used those characters. No runtime id
uses `.` or uppercase, so the runtime grammar above deliberately does NOT widen
to include them. This document is the shared reference both sides point at;
physically sharing the validator code is a later consolidation step.
