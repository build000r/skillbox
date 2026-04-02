# WORKGRAPH: journal_to_cm

## Nodes

### node-1: replace-emit-event
- **status:** done
- **depends_on:** []
- **writes:** `.env-manager/runtime_manager/shared.py`
- **context:** The journal system has two roles: structured JSONL event storage and ack-based agent memory. This node replaces the storage function with a plain-text logger. All other nodes depend on this because every caller imports `emit_event` from shared.py.
- **contract_excerpt:** Remove `emit_event`, `query_journal`, `read_acks`, `save_acks`, `is_acked`, `ack_events`, and related constants. Add `log_runtime_event(event_type, subject, detail=None, root_dir)` that writes `{ISO-8601} {type} {subject} {detail_json}` lines to `logs/runtime/runtime.log`. Best-effort, catches OSError.
- **acceptance_criteria:**
  - `emit_event` no longer exists in shared.py
  - `log_runtime_event` exists and writes plain text to runtime.log
  - All journal/ack functions and constants removed
  - No import errors from other modules (manage.py re-export updated)
- **done_when:** `python3 -c "from runtime_manager.shared import log_runtime_event"` succeeds AND `grep -c "emit_event\|query_journal\|read_acks\|ack_events" .env-manager/runtime_manager/shared.py` returns 0
- **validate_cmds:** `cd "$SKILLBOX_ROOT" && python3 -c "import sys; sys.path.insert(0,'.env-manager'); from runtime_manager.shared import log_runtime_event"`
- **rationale:** Foundation node — every other node imports from shared.py. Must land first.

### node-2: update-callers
- **status:** done
- **depends_on:** [node-1]
- **writes:** `.env-manager/pulse.py`, `.env-manager/runtime_manager/runtime_ops.py`, `.env-manager/runtime_manager/workflows.py`, `.env-manager/runtime_manager/context_rendering.py`
- **context:** 20+ call sites across 4 files import and call `emit_event`. Each must switch to `log_runtime_event` — same arguments, drop-in replacement. The manage.py re-export also needs updating since pulse.py imports from manage.
- **contract_excerpt:** Replace all `emit_event(...)` with `log_runtime_event(...)`. Update imports from `from manage import emit_event` to `from manage import log_runtime_event`. In context_rendering.py, also remove imports of `read_acks`, `query_journal`, `is_acked`.
- **acceptance_criteria:**
  - Zero occurrences of `emit_event` in any .env-manager Python file
  - All files import `log_runtime_event` where they previously imported `emit_event`
  - No import errors when loading any module
- **done_when:** `grep -r "emit_event" .env-manager/ --include="*.py"` returns 0 hits
- **validate_cmds:** `cd "$SKILLBOX_ROOT" && python3 -c "import sys; sys.path.insert(0,'.env-manager'); import pulse; from runtime_manager import runtime_ops, workflows, context_rendering"`
- **rationale:** Separate from node-1 because it touches 4 different files. Could run after node-1 lands.

### node-3: remove-mcp-tools
- **status:** done
- **depends_on:** [node-1]
- **writes:** `.env-manager/mcp_server.py`
- **context:** Three MCP tools expose the journal/ack system to agents. With the journal gone, these tools have no backing implementation and must be removed.
- **contract_excerpt:** Remove tool definitions for `skillbox_journal`, `skillbox_journal_write`, `skillbox_ack` from the TOOLS list. Remove handler functions `_handle_journal`, `_handle_journal_write`, `_handle_ack`. Remove dispatch branches and references in the `available_tools` list.
- **acceptance_criteria:**
  - `skillbox_journal`, `skillbox_journal_write`, `skillbox_ack` not in TOOLS list
  - Handler functions deleted
  - Dispatch function has no journal/ack branches
  - `skillbox_pulse` handler unchanged
- **done_when:** `grep -c "skillbox_journal\|skillbox_ack\|_handle_journal\|_handle_ack" .env-manager/mcp_server.py` returns 0
- **validate_cmds:** `cd "$SKILLBOX_ROOT" && python3 -c "import sys; sys.path.insert(0,'.env-manager'); import mcp_server; tools = [t['name'] for t in mcp_server.TOOLS]; assert 'skillbox_journal' not in tools; assert 'skillbox_pulse' in tools"`
- **rationale:** Parallel with node-2 — both depend on node-1 but don't touch overlapping files.

### node-4: remove-recent-activity
- **status:** done
- **depends_on:** [node-2]
- **writes:** `.env-manager/runtime_manager/context_rendering.py`
- **context:** The CLAUDE.md "Recent Activity" section reads from journal.jsonl via query_journal/read_acks/is_acked. With those functions gone and cm as the sole agent memory, this section is removed entirely.
- **contract_excerpt:** Delete lines 277-290 in context_rendering.py — the entire "Recent Activity" block that queries the journal, filters by acks, and renders unacked events into CLAUDE.md.
- **acceptance_criteria:**
  - No "Recent Activity" heading in generated CLAUDE.md
  - No imports of `read_acks`, `query_journal`, `is_acked` in context_rendering.py
  - "Attention" section (log error scanning) unchanged
- **done_when:** `grep -c "Recent Activity\|read_acks\|query_journal\|is_acked" .env-manager/runtime_manager/context_rendering.py` returns 0
- **validate_cmds:** `cd "$SKILLBOX_ROOT" && python3 -c "import sys; sys.path.insert(0,'.env-manager'); from runtime_manager.context_rendering import render_context_file"`
- **rationale:** Depends on node-2 because node-2 already touches context_rendering.py for the emit_event→log_runtime_event swap. This node removes the remaining journal imports and the Recent Activity block.

### node-5: verify-clean
- **status:** done
- **depends_on:** [node-2, node-3, node-4]
- **writes:** []
- **context:** Final verification that no dead code remains and the system functions correctly without the journal.
- **contract_excerpt:** N/A — verification only.
- **acceptance_criteria:**
  - Zero references to `journal.jsonl`, `journal.acks`, `emit_event`, `query_journal`, `read_acks`, `ack_events` in .env-manager/ Python files
  - MCP server starts without errors
  - Pulse daemon starts without import errors
  - Context rendering produces valid CLAUDE.md without "Recent Activity"
- **done_when:** All validate_cmds pass
- **validate_cmds:**
  - `cd "$SKILLBOX_ROOT" && grep -r "journal\.jsonl\|journal\.acks\|emit_event\|query_journal\|read_acks\|ack_events" .env-manager/ --include="*.py" | grep -v "^#" | wc -l` returns 0
  - `cd "$SKILLBOX_ROOT" && python3 -c "import sys; sys.path.insert(0,'.env-manager'); import mcp_server; import pulse; from runtime_manager import shared, runtime_ops, workflows, context_rendering; print('all imports clean')"`
- **rationale:** Catch any missed references before closing the slice.

## Execution Waves

```
Wave 1: [node-1]                    # Foundation: replace emit_event in shared.py
Wave 2: [node-2, node-3]            # Parallel: update callers + remove MCP tools
Wave 3: [node-4]                    # Remove Recent Activity (after node-2 touches context_rendering)
Wave 4: [node-5]                    # Verify clean
```
