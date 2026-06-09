# Session Support App — Design Spec

**Date:** 2026-06-09
**Status:** Approved design, ready for implementation planning

## Problem

When a Claude Code session has been running for a while — or when several
parallel sessions are open — it's hard to quickly reorient: *what was the last
thing I said? what is the agent's current response even about? am I drifting
from where this session started? should this have been a separate session?*
Today this means scrolling back through the conversation, which wastes time and
breaks focus.

The user has confirmed the **in-session** reorientation problem is the target —
not the parallel-windows problem (they know what each window is *for*).

## What already exists (and we will NOT rebuild)

The **quantitative** side is a crowded, solved space:

- Built-in: `/context` (context-window fill grid), `/usage` (session cost +
  billing window), and the customizable **statusline**.
- Third-party: `ccusage` (token/cost reports from local JSONL), Claude-Code-Usage-Monitor
  (live burn-rate predictions), various statusline scripts and context-bar extensions.

These are all **fuel gauges**. None answer the *semantic* questions above. The
novel, underserved angle is a **reorientation layer**, built on top of the
transcript logs. We reuse the gauges; we invent the orientation.

## Core design principle: two tiers, neither pollutes the working context

A key technical fact shapes everything: **the statusline runs entirely outside
the model's context** — its output is never added to the conversation. A
**skill**, by contrast, runs *through the model* and writes its output *into*
the conversation (costing context and inserting a topic-shift artifact).

Therefore:

- **Tier 1 — always-on signal → statusline.** Free, invisible to context, no
  artifacts. Carries one derived signal (*am I on track?*) plus cheap vanilla
  stats. Its job is to tell you *when* to look deeper, not to show everything.
- **Tier 2 — deliberate deep view → `/whereami` skill.** Rich analysis on
  demand. Pollution is neutralized by the **`Esc Esc` rewind hack**: run the
  skill, read the dump, then rewind the conversation back to the message right
  before you ran it. The skill output and its invocation both vanish; context is
  restored. The skill is strictly **read-only** so there are no side effects to
  lose on rewind.

## Data source

The session transcript JSONL. The statusline script is fed a JSON blob on stdin
that includes `transcript_path`, `session_id`, `cwd`, `model`, and cost/duration
fields — so the current session's file is always known unambiguously. Each line
of the JSONL is one timestamped event (user message, assistant message, tool
call, etc.).

## Components

Three small, independently-testable pieces, each with one job.

### 1. Statusline renderer

- **Trigger:** every statusline tick (frequent — must be instant).
- **Inputs:** stdin JSON payload + the drift cache file for this `session_id`.
- **Does NO network I/O.** Only reads the cache file and tails the JSONL.
- **Renders one line:**
  - 🟢🟡🔴 coherence light (read from cache)
  - truncated last user message (~60 chars, tailed live from the JSONL) —
    solves "what did I just say" with zero action
  - ⏱ session elapsed (from `total_duration_ms`)
  - 🔢 context-window fill `%` / token count *if exposed by the installed CC
    version's payload*; otherwise omit and defer to native `/context`
  - 💲 cost (from `total_cost_usd`) — **optional**; see risk (D)

The vanilla stats (elapsed, cost, and context-% where available) are nearly free
because the payload already contains them — no transcript parsing required.

**Critical correctness notes (see Risks):** the renderer must (a) resolve the
*last genuine human turn*, not the last JSONL line — Claude Code logs tool
results and injected context as `user`-role entries too — and (b) read the file
by seeking from the end, never parsing the whole transcript on each tick.

### 2. Drift sidecar

- **Trigger:** a `Stop` hook, which fires only when the agent finishes a turn.
  An idle session produces no events, so there is never an idle ping.
- **Throttle:** message-count only (no timer). Recompute every **N assistant
  turns** (default **4**). State (turn count at last compute) is tracked in the
  cache file.
- **On run:**
  1. Read the transcript JSONL.
  2. Extract the **opening goal** (first 1–2 genuine human turns) and the
     **recent messages** (last few human turns) — filtering out tool-result
     entries and stripping injected `<system-reminder>` / CLAUDE.md / hook
     context blocks so the model scores real conversation, not harness noise.
  3. Make one **Haiku** call (Anthropic SDK) scoring topic drift `0–100` plus a
     one-line label.
  4. Write `~/.claude/whereami/<session-id>.json`:
     `{ score, label, ts, msg_count, opening_goal }`.
- **Must not block the turn:** the `Stop` hook should fire the Haiku call
  detached/in the background and return immediately, so it never adds latency
  before the user can type again (see risk (B)).
- **Cost:** pennies, in the background, never touches the working context.

### 3. `/whereami` skill

- **Read-only**, deliberate, self-cleaning via `Esc Esc`.
- **Produces the rich view:**
  - full last user message (verbatim)
  - original goal vs. what's happening now
  - the open loop / what's-your-turn
  - drift explanation
  - the "should you split this session" recommendation
- Uses the in-context conversation plus the transcript file (for timestamps /
  exact first message). Because the model is already in the loop, this incurs no
  extra API cost beyond the turn itself.

## Wiring

`settings.json` gains:

- a `statusLine` command pointing at the renderer
- a `Stop` hook invoking the drift sidecar

Implementation language: **Python 3.9+** (avoid `X | Y` unions; use
`Optional`/`Union`). Haiku via the Anthropic SDK.

## Cache schema

`~/.claude/whereami/<session-id>.json`:

```json
{
  "score": 0,
  "label": "string, one line",
  "ts": "iso-8601 timestamp",
  "msg_count": 0,
  "opening_goal": "string"
}
```

## Light thresholds (tunable)

- 🟢 green: 0–33
- 🟡 amber: 34–66
- 🔴 red: 67–100

## Scope boundaries (YAGNI)

- **No** reimplementation of `/context` — surface a compact number only.
- **No** parallel-multi-session dashboard — out of scope; the in-session need is
  the target.
- **No** time-based throttle — message-count only.
- Haiku calls live **only** in the throttled `Stop` hook, **never** in the
  statusline.

## Testing

- **Renderer:** feed sample stdin payloads + cache fixtures; assert the rendered
  line. Cover missing-cache, empty-last-message, and missing-context-field cases.
- **Sidecar:** feed sample transcripts; assert throttle (skips before N turns,
  recomputes at N) and cache-write shape. Mock the Haiku call.
- **Skill:** manual/read-only; verify it touches no files and produces the five
  sections from a sample transcript.

## Implementation risks (found in design review)

- **(A) "Last user message" is not the last JSONL line.** Claude Code records
  tool results and injected context as `user`-role entries. Both the renderer
  (last-said) and the sidecar (opening goal) must resolve the last/first
  *genuine human turn* and strip injected `<system-reminder>`/CLAUDE.md/hook
  blocks. Getting this wrong means the statusline shows a tool result instead of
  what you actually said. **Highest-risk item.**
- **(B) The `Stop` hook must not block.** A synchronous Haiku call would delay
  the prompt returning to you. Background it (detached) and return immediately.
- **(C) Renderer must be fast.** The statusline re-renders often; reading the
  whole transcript each tick would lag the UI. Seek from the end of the file.
- **(D) `total_cost_usd` may be meaningless on subscription plans.** For
  Max/Pro it can read `$0` or be misleading. Make the 💲 segment optional /
  config-gated rather than always-on; elapsed and context-% are the reliably
  useful stats.

## Open / deferred tuning

- Exact throttle N (default 4) — adjust live.
- Whether the installed CC version exposes context-window fill in the statusline
  payload — verify during implementation; do not overpromise.
- Light thresholds — adjust after real use.
