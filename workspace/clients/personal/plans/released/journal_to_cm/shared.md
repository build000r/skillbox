# journal_to_cm — API Contract

> No new API endpoints. This slice removes 3 MCP tools and replaces 1 internal function.

## MCP Tools Removed

### `skillbox_journal` — REMOVED
Previously: Query event journal with filters (since, event_type, subject, limit).
Replacement: Agents use `cm context "<task>"` for session-relevant history.

### `skillbox_journal_write` — REMOVED
Previously: Agents write events for cross-session continuity.
Replacement: Agents use `cm add "<rule>"` to persist learnings.

### `skillbox_ack` — REMOVED
Previously: Acknowledge events to hide them from CLAUDE.md Recent Activity.
Replacement: No replacement needed — Recent Activity section is removed entirely.

## MCP Tools Unchanged

- `skillbox_pulse` — reads `pulse.state.json`, not the journal. No changes.
- `skillbox_session_start/end/event/resume/status` — session lifecycle continues to work. The `emit_event` calls in session code switch to `log_runtime_event` but session MCP tools are unaffected.
- All other skillbox MCP tools — no changes.

## Internal Function Contract

### `log_runtime_event()` (replaces `emit_event()`)

```
log_runtime_event(
    event_type: str,
    subject: str,
    detail: dict | None = None,
    root_dir: Path = DEFAULT_ROOT_DIR,
) -> None
```

**Behavior:**
- Appends one line to `{root_dir}/logs/runtime/runtime.log`
- Format: `{ISO-8601-timestamp} {event_type} {subject} {detail_json_if_present}`
- Best-effort (catches OSError, never breaks caller)
- Creates parent directory if missing

**Example output lines:**
```
2025-01-15T14:32:05 pulse.service_crashed myservice {"from":"running","to":"down"}
2025-01-15T14:32:06 pulse.restarted myservice {"reason":"crash recovery","pid":12345}
2025-01-15T14:33:00 sync.completed runtime {"action_count":3}
2025-01-15T14:33:01 context.generated personal/focus {}
```

## Files Removed (no longer written)

- `logs/runtime/journal.jsonl` — structured event journal
- `logs/runtime/journal.acks.json` — acknowledgement store

## CLAUDE.md Context Changes

The "Recent Activity" section is removed from generated CLAUDE.md content. The "Attention" section (log error scanning) remains unchanged.
