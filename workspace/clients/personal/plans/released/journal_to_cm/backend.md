# journal_to_cm — Backend Spec

## Files Modified

### `.env-manager/runtime_manager/shared.py`

**Remove:**
- `JOURNAL_REL` constant (line 586)
- `emit_event()` function (lines 589-608)
- `query_journal()` function (lines 611-640)
- `ACKS_REL`, `DEFAULT_ACK_EXPIRY_HOURS` constants (lines 647-648)
- `read_acks()` function (lines 651-659)
- `save_acks()` function (lines 662-666)
- `is_acked()` function (lines 669-683)
- `ack_events()` function (lines 686-724+)

**Add:**
- `RUNTIME_LOG_REL = Path("logs") / "runtime" / "runtime.log"`
- `log_runtime_event(event_type, subject, detail=None, root_dir=DEFAULT_ROOT_DIR)` — plain text line writer per shared.md contract

### `.env-manager/mcp_server.py`

**Remove from TOOLS list:**
- `skillbox_journal` tool definition (~line 538)
- `skillbox_journal_write` tool definition (~line 576)
- `skillbox_ack` tool definition (~line 604)

**Remove handler functions:**
- `_handle_journal()` 
- `_handle_journal_write()`
- `_handle_ack()`

**Remove from dispatch:**
- Journal/ack branches in the dispatch function (~line 890-895)
- Journal/ack names from the `available_tools` list (~line 902)

### `.env-manager/pulse.py`

**Change:**
- Import `log_runtime_event` instead of `emit_event` from manage
- All `emit_event(...)` calls become `log_runtime_event(...)` — same arguments, drop-in replacement
- Update module docstring to reference `runtime.log` instead of `journal.jsonl`

### `.env-manager/runtime_manager/runtime_ops.py`

**Change:**
- All `emit_event(...)` calls become `log_runtime_event(...)` (lines 640, 1301, 1317, 1326, 1334, 1403, 1414, 1463)

### `.env-manager/runtime_manager/workflows.py`

**Change:**
- All `emit_event(...)` calls become `log_runtime_event(...)` (lines 163, 1191)

### `.env-manager/runtime_manager/context_rendering.py`

**Remove:**
- Import of `read_acks`, `query_journal`, `is_acked` from shared
- Lines 277-290: entire "Recent Activity" block

**Change:**
- `emit_event(...)` calls become `log_runtime_event(...)` (lines 346, 440)

## Business Rules

1. `log_runtime_event` is best-effort — it must never raise or break the caller
2. Log format is human-readable, not machine-parseable — no consumer depends on structure
3. No log rotation in this slice — runtime.log grows unbounded (acceptable for single-tenant debug log; rotation is out of scope)

## Permissions / Access Control

No changes. `log_runtime_event` has the same filesystem access pattern as `emit_event` (write to logs/ directory).

## Edge Cases

- **Existing journal.jsonl / journal.acks.json on disk:** Ignored. No code reads them. User can delete manually.
- **Agents calling removed MCP tools:** MCP server returns `unknown_tool` error with available tools list. This is the existing behavior for any unknown tool name.
- **manage.py `emit_event` export:** `manage.py` re-exports from `shared.py`. The import in pulse.py (`from manage import emit_event`) must change to `from manage import log_runtime_event`. Check that `manage.py`'s `__all__` or re-export is updated.
