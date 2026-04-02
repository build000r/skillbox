# journal_to_cm

> Replace the bespoke event journal + ack system with cm as the sole agent memory interface.

## Core Value Gate

- **Primary actor:** Agent (Claude Code / Codex) starting a session
- **User-visible outcome:** Agents get session-relevant context from cm instead of a hand-rolled JSONL journal — one memory system instead of two
- **Minimum winning slice:** Remove agent-facing journal MCP tools, drop the CLAUDE.md "Recent Activity" injection, simplify pulse logging to plain text
- **Explicit non-goals:** Changing cm itself, adding new cm features, modifying pulse reconciliation logic, changing session management, adding new context sections to CLAUDE.md
- **Debt avoided:** No more maintaining two parallel memory systems with different decay semantics; no JSONL rotation/cleanup; no ack expiry logic to reason about

## User Stories

### S1: Agent uses cm for session context (replaces journal read + ack)

As an agent starting a session, I get relevant context from `cm context "<task>"` instead of reading unacked journal events in CLAUDE.md, so I have one coherent memory system with proper confidence decay instead of a bespoke 24h ack expiry.

**Acceptance criteria:**
- CLAUDE.md no longer contains a "Recent Activity" section
- No MCP tools named `skillbox_journal`, `skillbox_journal_write`, or `skillbox_ack` are registered
- `cm context` continues to work as before (no changes to cm)

**Test scenarios:**
- Generate CLAUDE.md context → no "Recent Activity" heading present
- List MCP tools → journal/ack tools absent
- `cm context "check runtime"` → returns rules and history (unchanged behavior)

### S2: Pulse logs operationally without structured journal (replaces emit_event)

As a developer debugging pulse, I can read a plain-text log of pulse activity without needing to parse JSONL or understand the ack system.

**Acceptance criteria:**
- `emit_event()` replaced with a simple `log_runtime_event()` that appends human-readable lines to `logs/runtime/runtime.log`
- All existing callers (pulse.py, runtime_ops.py, workflows.py, context_rendering.py) switched to the new function
- `journal.jsonl` and `journal.acks.json` are no longer written to

**Test scenarios:**
- Pulse detects a crashed service → line appears in `runtime.log` like `2025-01-15 14:32:05 pulse.service_crashed myservice from=running to=down`
- No `journal.jsonl` file is created after fresh start
- Existing `journal.jsonl` / `journal.acks.json` can be safely deleted (no code references them)

### S3: Dead code removal

As a maintainer, the codebase has no orphaned journal/ack code.

**Acceptance criteria:**
- `query_journal`, `read_acks`, `save_acks`, `is_acked`, `ack_events` removed from `shared.py`
- `JOURNAL_REL`, `ACKS_REL`, `DEFAULT_ACK_EXPIRY_HOURS` constants removed
- MCP handler functions `_handle_journal`, `_handle_journal_write`, `_handle_ack` removed from `mcp_server.py`
- Journal/ack tool definitions removed from `TOOLS` list in `mcp_server.py`
- `skillbox_pulse` MCP tool stays (it reads pulse.state.json, not the journal)

**Test scenarios:**
- `grep -r "journal\|ack_events\|read_acks\|query_journal" .env-manager/` returns zero hits (excluding comments/changelog)
- `python3 -c "from runtime_manager.shared import emit_event"` fails (function removed)
- `python3 -c "from runtime_manager.shared import log_runtime_event"` succeeds

## Trade-offs

| Decision | Chosen | Alternative | Why Not |
|----------|--------|-------------|---------|
| Drop Recent Activity entirely | No CLAUDE.md section | Replace with `cm context --brief` injection | Agents already run `cm context` at session start per cass-memory skill protocol; injecting it into CLAUDE.md is redundant and couples context rendering to cm |
| Plain text log | `runtime.log` with human-readable lines | Keep JSONL for machine parsing | No consumer needs structured queries anymore — journal MCP tools are gone. Plain text is easier to tail/debug |
| Clean cut (no bridge) | Remove all 3 MCP tools at once | Keep `skillbox_journal` read-only for operational queries | Agents should use `cm` for memory and `skillbox_pulse` for operational state. A read-only journal tool is a half-measure that preserves dead complexity |
| Keep `emit_event` call sites | Replace function, keep call sites | Remove event logging from non-pulse callers | Operational logging from runtime_ops/workflows is still useful for debugging, just doesn't need to be agent-facing |

## Rejected Approaches

1. **Gradual migration with dual-write** — Write to both journal and cm during a transition period. Rejected: cm is already working and the journal has no consumers once the MCP tools are removed. Dual-write adds complexity for zero benefit.
2. **Convert journal events to cm rules** — Import historical journal entries as cm playbook rules. Rejected: journal events are operational (crashes, restarts) not procedural knowledge. They don't map to cm's rule model.
3. **Keep JSONL, just remove acks** — Simplify by dropping ack logic but keeping structured events. Rejected: without agent-facing query tools, there's no consumer for structured events. Plain text is simpler.

## Out of Scope

- cm feature changes (confidence decay tuning, new commands)
- Pulse reconciliation logic changes
- Session management changes (session start/end/resume still works, just emits to runtime.log instead of journal)
- Log rotation for runtime.log (can be added later if needed)
- Migration of historical journal.jsonl data
