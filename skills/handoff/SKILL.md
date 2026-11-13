---
name: handoff
description: Wrap up a long session so the next one can start small. Use when the user asks to hand off, wrap up, rotate or continue in a new session, or says the context is getting long.
---

# Hand off to a fresh session

Every turn re-sends the whole conversation, so a long session costs far more per turn than a short one. Handing off moves what matters into memory so a new session can start from a small brief.

1. Save the state of the work with `memory_save`, type `status`, reusing the existing status id for this workstream if there is one. Include: what was done, what is in progress, the exact next step, and the files involved.
2. Save any decisions, confirmed fixes and failed approaches from this session that are not yet in memory, each as its own memory with checks where possible.
3. Call `memory_verify` so nothing stale is carried over.
4. Call `context_compile` with the next step as the task, and show the user the result so they can see what the next session will start with.
5. Tell the user to start a new session and say what to type to continue, for example "continue with <next step>".

Do not summarise the whole conversation. Save only what the next session needs.
