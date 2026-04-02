# journal_to_cm - Completion Summary

**Consolidated:** 2026-04-02
**Domain:** [runtime]
**Stories:** 3✓ 1→

## What Users Can Now Do

- **Agents get session context from cm** - One coherent memory system with confidence decay instead of a bespoke 24h ack-expiry journal
- **Developers debug pulse with plain-text logs** - `tail -f logs/runtime/runtime.log` instead of parsing JSONL and understanding ack semantics
- **Maintainers have a simpler codebase** - ~430 lines of journal/ack infrastructure removed, one memory system instead of two

## What's Deferred

### Log rotation for runtime.log
- **Why:** Out of scope for the migration — runtime.log will grow unbounded until rotation is added
- **Impact:** On long-running boxes, runtime.log could grow large; operators would need to manually truncate
- **See also:** TBD

## Key Decisions

- **Clean cut over gradual migration** - No dual-write bridge period. cm was already working; the journal had no consumers once MCP tools were removed. Simpler and lower risk.
- **Plain text over JSONL** - No consumer needs structured queries anymore. Plain text is easier to tail and debug.
- **Drop Recent Activity entirely** - Agents already run `cm context` at session start per cass-memory skill protocol; injecting it into CLAUDE.md would be redundant.

## Gotchas

- Existing `journal.jsonl` and `journal.acks.json` files remain on disk after this change. They are inert (nothing reads or writes them) and can be safely deleted.
- The `skillbox_pulse` MCP tool is intentionally preserved — it reads `pulse.state.json`, not the journal.

## Related Slices

None currently. This is the only [runtime] slice.

## References

- **API Contract:** [shared.md](./shared.md)
- **Database Schema:** [schema.mmd](./schema.mmd)
- **Original Plan:** [_archived/plan.md](./_archived/plan.md)
