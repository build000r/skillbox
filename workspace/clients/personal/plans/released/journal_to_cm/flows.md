# journal_to_cm — Flows

## Flow 1: Agent Session Start (Before → After)

### Before
```
Agent starts session
  → CLAUDE.md loaded (contains "Recent Activity" from journal)
  → Agent reads unacked events
  → Agent investigates issues
  → Agent calls skillbox_ack to dismiss resolved events
  → Agent calls cm context for procedural memory
  → Two memory systems consulted, different formats
```

### After
```
Agent starts session
  → CLAUDE.md loaded (no "Recent Activity" section)
  → Agent calls cm context "<task>" per cass-memory skill protocol
  → One memory system, consistent format
```

## Flow 2: Pulse Crash Detection (Before → After)

### Before
```
Pulse detects service crash
  → emit_event("pulse.service_crashed", ...) writes to journal.jsonl
  → Next CLAUDE.md render: event appears in "Recent Activity"
  → Agent reads it, investigates, acks it
```

### After
```
Pulse detects service crash
  → log_runtime_event("pulse.service_crashed", ...) writes to runtime.log
  → Developer can tail runtime.log for debugging
  → Agent uses cm context / skillbox_pulse for operational awareness
```

## Flow 3: Agent Records a Learning (Before → After)

### Before
```
Agent discovers something worth remembering
  → Calls skillbox_journal_write to write event
  → Event appears in next session's "Recent Activity"
  → Next agent reads it, maybe acks it
```

### After
```
Agent discovers something worth remembering
  → Calls cm add "rule text" per cass-memory protocol
  → Rule enters playbook with confidence tracking + decay
  → Next agent gets it via cm context if relevant
```

## State Transition: MCP Tool Availability

```
Before: [skillbox_journal, skillbox_journal_write, skillbox_ack, skillbox_pulse, ...]
After:  [skillbox_pulse, ...]
         ^^^^^^^^^^^^^^^^
         operational state tool stays
```
