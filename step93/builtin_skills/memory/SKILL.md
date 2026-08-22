---
name: memory
description: Two-layer memory system with Dream-managed knowledge files.
always: true
---

# Memory

This skill is marked `always: true`, so its full content is injected into the
system prompt (the `# Active Skills` section) instead of only a summary line.

## Structure

- `SOUL.md` — Bot personality and communication style. **Managed by Dream.** Do NOT edit.
- `USER.md` - User profile and preferences. **Managed by Dream.** Do NOT edit.
- `memory/MEMORY.md` - Long-term facts. **Managed by Dream.** Do NOT edit.
- `memory/history.jsonl` - append-only JSONL, not loaded into context.