---
name: whereami
description: Read-only in-session reorientation. Use when you (the user) have lost track of a long or resumed session and want a quick rich summary of what's going on — what you last said, the original goal vs. now, the open loop, drift, and whether to split the session. Tip: after reading, press Esc Esc to rewind this skill out of the conversation so it leaves no artifact.
---

# /whereami — Where am I in this session?

You are producing a **read-only** reorientation snapshot. Do NOT edit, write, or
create any files. Do NOT run commands with side effects. The user will likely
`Esc Esc` to rewind this output away afterward, so it must leave nothing behind.

Using the conversation already in your context (and, only if useful for exact
timestamps, reading the current session's transcript file), output these five
sections, concise and skimmable:

## 1. Last thing you said
Quote the user's most recent genuine message verbatim (not tool results).

## 2. Original goal vs. now
One line on what this session set out to do (from the first real user
message), and one line on what it's actually doing now.

## 3. Open loop / your turn
What is the agent currently waiting on or what did the last response ask of
the user? What is the next concrete action?

## 4. Drift
State whether the conversation has stayed on its original path or wandered, and
how far. If an orientation cache exists at `~/.claude/whereami/<session-id>.json`,
you may read its `score`, `gist`, `open_loop`, and `goal` fields, but do not
write to it.

## 5. Split recommendation
A clear call: keep going in this session, or start a fresh one — with a
one-sentence reason (e.g. context is full, topic has fully changed, original
task is done).

Keep the whole thing tight — this is a glance, not a report.
