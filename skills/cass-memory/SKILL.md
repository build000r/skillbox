---
name: cass-memory
description: >-
  CASS Memory System (cm) for procedural memory. Use when starting non-trivial
  tasks, learning from past sessions, building playbooks, or preventing repeated
  mistakes via trauma guard.
---

<!-- TOC: Quick Start | THE EXACT PROMPT | Architecture | Commands | References -->

# cass-memory — CASS Memory System (cm)

> **Core Capability:** Transforms scattered agent sessions into persistent, cross-agent procedural memory. A pattern discovered in Cursor **automatically** helps Claude Code on the next session.

## Quick Start

```bash
# Initialize with a starter playbook
cm init --starter typescript

# THE ONE COMMAND: run before any non-trivial task
cm context "implement user authentication" --json

# Check system health
cm doctor --json
```

---

## Session Start

Before any non-trivial task:

```bash
cm context "<task description>" --json
```

Read the output:
- `relevantBullets`: Playbook rules scored by relevance
- `antiPatterns`: Things that caused problems before
- `historySnippets`: Past sessions (yours and other agents')
- `suggestedCassQueries`: Deeper investigation if needed

Reference rule IDs when following them (e.g., "Following b-8f3a2c...")

---

## Rule Feedback

```bash
# When a rule helped
cm mark b-8f3a2c --helpful

# When a rule caused problems
cm mark b-xyz789 --harmful --reason "Caused regression"
```

---

## THE EXACT PROMPT — Trauma Guard Setup

```
# Install safety hooks to prevent dangerous commands
cm guard --install       # Claude Code hook
cm guard --git          # Git pre-commit hook
cm guard --status       # Check installation

# Add custom trauma patterns
cm trauma add "DROP TABLE" --description "Mass deletion" --severity critical

# Scan past sessions for trauma patterns
cm trauma scan --days 30
```

---

## Three-Layer Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                    EPISODIC MEMORY (cass)                           │
│   Raw session logs from all agents — the "ground truth"             │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ cass search
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    WORKING MEMORY (Diary)                           │
│   Structured session summaries: accomplishments, decisions, etc.    │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ reflect + curate (automated)
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    PROCEDURAL MEMORY (Playbook)                     │
│   Distilled rules with confidence tracking and decay                │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Essential Commands

| Command | Purpose |
|---------|---------|
| `cm context "<task>" --json` | Get rules + history for task |
| `cm mark <id> --helpful/--harmful` | Record feedback |
| `cm playbook list` | View all rules |
| `cm top 10` | Top effective rules |
| `cm doctor --json` | System health |
| `cm guard --install` | Install safety hooks |

---

## Agent Protocol

```
1. START:    cm context "<task>" --json
2. WORK:     Reference rule IDs when following them
3. FEEDBACK: cm mark <id> --helpful or --harmful when rules help/hurt
4. LEARN:    cm add "rule text" when you discover something worth keeping
```

**No external LLM needed.** `CASS_MEMORY_LLM=none` is set. You (Claude Code /
Codex) are the reflectors — add rules directly with `cm add` instead of relying
on `cm reflect` to call an external API.

To add rules from session learnings:

```bash
cm add "Always check token expiry before debugging auth timeouts"
cm add --category security "Never store session tokens in localStorage"
```

---

## Confidence Decay

Rules aren't immortal. Confidence decays without revalidation:

| Mechanism | Effect |
|-----------|--------|
| **90-day half-life** | Confidence halves every 90 days without feedback |
| **4x harmful multiplier** | One mistake counts 4x as much as one success |
| **Maturity progression** | `candidate` → `established` → `proven` |

---

## Anti-Pattern Learning

Bad rules don't just get deleted. They become warnings:

```
"Cache auth tokens for performance"
    ↓ (3 harmful marks)
"PITFALL: Don't cache auth tokens without expiry validation"
```

---

## Starter Playbooks

```bash
cm starters                    # List available
cm init --starter typescript   # Initialize with starter
cm playbook bootstrap react    # Apply to existing playbook
```

| Starter | Focus |
|---------|-------|
| **general** | Universal best practices |
| **typescript** | TypeScript/Node.js |
| **react** | React/Next.js |
| **python** | Python/FastAPI/Django |
| **rust** | Rust service patterns |

---

## Token Budget Management

| Flag | Effect |
|------|--------|
| `--limit N` | Cap number of rules |
| `--min-score N` | Only rules above threshold |
| `--no-history` | Skip historical snippets |
| `--json` | Structured output |

---

## Graceful Degradation

| Condition | Behavior |
|-----------|----------|
| No cass | Playbook-only scoring, no history |
| No playbook | Empty playbook, commands still work |
| No LLM | Deterministic reflection |
| Offline | Cached playbook + local diary |

---

## Installation

```bash
brew install dicklesworthstone/tap/cm
cm init
```

Set LLM-free mode (reflection done by your agents, not an external API):

```bash
export CASS_MEMORY_LLM=none  # add to ~/.zshrc
```

---

## Troubleshooting

| Error | Solution |
|-------|----------|
| `cm: command not found` | `brew install dicklesworthstone/tap/cm` |
| `cass not found` | Install cass (`brew install cass`) |
| `Playbook corrupt` | Run `cm doctor --fix` |

---

## References

| Topic | Reference |
|-------|-----------|
| Full command reference | [COMMANDS.md](references/COMMANDS.md) |
| Cognitive architecture | [ARCHITECTURE.md](references/ARCHITECTURE.md) |
| Trauma guard system | [TRAUMA-GUARD.md](references/TRAUMA-GUARD.md) |
| MCP server integration | [MCP-SERVER.md](references/MCP-SERVER.md) |
| Onboarding workflow | [ONBOARDING.md](references/ONBOARDING.md) |
