# whereami v2 — Orientation Panel — Design Spec

**Date:** 2026-06-09
**Status:** Approved design, ready for implementation planning
**Supersedes:** extends `2026-06-09-session-support-app-design.md` (v1, shipped)

## Problem (v1 pain points, observed in practice)

1. **The truncated last message is too short.** 60 chars often cuts the
   message before its meaning; the user wants the whole thing (or close).
   Hover is not an option: no statusline surface (CLI, desktop app, VS Code
   extension) has any hover/tooltip affordance.
2. **The colored dot doesn't orient.** A green/amber/red `●` tells you a
   judgment without telling you anything. A *written* message is more useful
   than a colored glyph — v1 already computes a 3–6 word label every 6 turns
   and then throws it away at render time.
3. **The `Esc Esc` ritual around `/whereami` is clunky.** Skills necessarily
   run through the model and pollute context; the statusline is the only
   documented context-free output surface. So the graceful fix must live
   *outside* the conversation.

## Verified platform facts (current docs, CC 2.1.170)

- **Multi-line statuslines are supported** — each stdout line renders as its
  own row.
- **No hover anywhere**; ANSI passes through; OSC 8 hyperlinks pass through
  and are Cmd+clickable (Ghostty supports them). Deferred, not used in v2.
- **`refreshInterval`** re-runs the statusline command on a timer in addition
  to event-driven renders (events: each assistant message, `/compact`,
  permission-mode change; debounced 300ms). Exact field name/units to be
  verified against docs at implementation time.
- The statusline payload includes `context_window` (already used), plus
  workspace/git/model fields (available; deliberately unused in v2).

## Core idea

**Precompute the whole orientation panel in the sidecar; render it two ways.**
The single throttled Haiku call stops being a drift-scorer and becomes an
orienter: it produces a *gist* ("what is this session doing right now"), an
*open loop* ("what is the agent waiting on from me"), and the drift score.
The statusline renders a compact two-line view always, and an expanded panel
on demand ("peek") — both read-only, instant, network-free, zero context cost.
`/whereami` survives unchanged in role (the model's own in-context read) but
stops being the daily driver, which is what makes `Esc Esc` graceful: you
mostly stop needing it.

## Components (deltas from v1)

### 1. Drift sidecar → orientation sidecar (`drift.py`)

Same trigger (Stop hook), same throttle (`THROTTLE_TURNS = 6`), same detached
spawn, same transport (logged-in `claude` CLI, Haiku, no API key).

**Prompt inputs:**
- opening goal (cached, else `opening_turns`)
- recent genuine human turns (last ~4, as v1)
- **new:** the last assistant message (`transcript.last_assistant_text()`,
  truncated to ~700 chars) — required for the open-loop line

**Model contract** — reply is exactly one JSON object:

```json
{"score": 0-100, "gist": "3-8 words: what the session is doing right now",
 "open_loop": "one line: what the agent is waiting on from the user"}
```

Parsing stays tolerant exactly as v1 (regex-extract the object, clamp score,
empty-string defaults on junk). The `label` field is **dropped**; `gist`
replaces it. Old caches containing `label` are simply ignored by the renderer
(no migration; caches are per-session ephemera).

### 2. Cache schema v2 (`~/.claude/whereami/<session-id>.json`)

```json
{
  "score": 58,
  "gist": "CI retry backoff logic",
  "open_loop": "choose a backoff strategy",
  "ts": "iso-8601",
  "opening_goal": "first human turns, joined",
  "turns_seen": 12,
  "turns_at_last_compute": 12,
  "history": [["iso-8601", 41], ["iso-8601", 58]]
}
```

`history` appends one `(ts, score)` pair per compute, capped at the most
recent 50 entries. Nothing renders it in v2 — it exists so trend/sparkline
rendering later needs no migration.

### 3. Statusline, normal mode — two lines (`statusline.py`)

```
parser retry logic · ⏱ 42m · ⊠ 63%
❯ ok now let's make the retry logic exponential with jitter and cap it at 5
```

- **Line 1:** the gist in **colored words** — green `0–33`, amber `34–66`,
  red `67–100` (same thresholds, still tunable). No dot. Before the first
  score: a dim `…` placeholder. Then the existing gauges: `⏱` elapsed,
  `⊠` context-%, env-gated cost (all unchanged).
- **Line 2:** `❯ ` + last genuine human message, truncated at ~150 chars
  (constant with env override `WHEREAMI_MSG_LIMIT`). Omitted entirely when
  there is no last message — the render degrades to one line.
- Still no network I/O; reads = cache file + transcript tail + peek-file stat.

### 4. Peek mode — the hover replacement

- New console script **`whereami-peek`**: touches `~/.claude/whereami/peek`.
  Bound by the user to a global hotkey (Raycast/Hammerspoon/second pane —
  user-side, out of scope). The file is **global by design**: a hotkey cannot
  know which session is focused, and expanding every session's statusline is
  harmless since only the focused window is visible.
- The renderer treats peek as **active while the peek file's mtime is younger
  than `PEEK_SECONDS = 30`** — auto-collapse is just the mtime aging out; no
  state to clean up, no toggle-off required.
- **Expanded panel** (replaces the normal two lines while active):

```
drift 58 · CI retry backoff logic   (goal: in-session reorientation tool)
❯ ok now let's make the retry logic exponential with jitter and cap it at 5
⊙ your turn: choose a backoff strategy
⏱ 42m · ⊠ 63% · scored 2 turns ago
```

  Line 1 colored as in normal mode; goal recap truncated; line 2 uses a
  longer limit (~200); `⊙` line omitted if no `open_loop` yet; staleness
  ("scored N turns ago") derived locally from
  `turns_seen − turns_at_last_compute`, or "no score yet".
- **Split heuristic (local, no model):** when `score > 66` **and**
  context-% `> 70`, the panel's last line appends `· consider splitting`.
  Thresholds tunable.

### 5. Freshness: peek-triggered recompute (guarded)

When the renderer sees peek active **and** `turns_seen >
turns_at_last_compute`, it spawns the existing detached compute for its own
session — process spawn is ~ms, the renderer itself stays network-free.
Guard against stacking (the renderer may tick many times while peek is
active): before spawning, create a marker file
`<session-id>.computing`; skip the spawn if a marker younger than 120s
exists; the compute process removes the marker when done. `Popen` failures
are swallowed — rendering must never break because a spawn failed.

The panel therefore opens instantly with cached data and self-corrects on a
subsequent tick. The scheduled Haiku budget is unchanged (every 6 turns);
extra computes are bounded by actual peek presses — relevant because
`claude -p` calls draw on the user's Pro/Max rate-limit window.

### 6. Wiring (delta)

- `pyproject.toml`: add the `whereami-peek` console script.
- Global `statusLine` settings entry gains a `refreshInterval` (~3s) so the
  panel collapses and refreshes while the session is idle. **This is an edit
  to the user's global settings.json wiring — flag at install time, never
  silently.** The statusline command itself is unchanged (same console
  script), so the editable-venv install keeps working with no re-wiring.
- Stop hook: unchanged.
- `.claude/skills/whereami/SKILL.md`: minor wording update so the drift
  section reads `score`/`gist`/`open_loop` from the cache; role unchanged
  (read-only, Esc-Esc-friendly).

## Scope boundaries (YAGNI)

- **No** external `whereami` CLI and **no** `--all` multi-session dashboard
  (v3 candidates; the per-session caches this version builds are their data
  source when the time comes).
- **No** sparkline/trend *rendering* — only the `history` data collection.
- **No** OSC 8 links, **no** git-branch/model segments (payload has them;
  declined again).
- **No** time-based compute throttle — compute stays turn-count + demand
  (peek) driven; the peek-file mtime is a UI timer, not a compute trigger.
- Haiku calls remain **only** in detached child processes — never in the
  renderer process itself.

## Implementation risks

- **(A) Gist staleness mid-burst.** The "now:" line can describe up to 6
  turns ago. Mitigations: line 2 (last message) is always live from the
  transcript tail, and peek triggers a recompute. Accepted residual risk.
- **(B) Spawn stacking from the renderer.** A 3s refresh during a 30s peek
  is ~10 ticks; without the marker guard each would spawn a compute. The
  marker file is load-bearing — test it explicitly.
- **(C) Narrow terminals.** Two generous lines may wrap and push the layout.
  Truncation limits are constants with env overrides; tune in real use.
- **(D) `refreshInterval` schema drift.** Field name/units verified against
  current docs during implementation, as v1 did for `context_window`.

## Testing

- **Sidecar:** prompt-build includes last assistant text; 3-field JSON
  parse fixtures (junk, missing fields, clamping); history append + 50-cap.
- **Renderer:** goldens for normal 2-line, 1-line degraded, dim-placeholder,
  peek panel (with/without open_loop, split hint, staleness text); peek
  activation via injected clock + tmp peek file; spawn-guard (marker
  present → no spawn; stale marker → spawn; Popen raising → render still
  returns).
- **Transcript:** `last_assistant_text` (string + block content, skips
  non-assistant entries, missing file → None).
- All offline; runners stubbed as in v1. Python 3.9+ typing throughout.

## Open / deferred tuning

- `PEEK_SECONDS = 30`, message limits (150/200), marker TTL (120s), history
  cap (50), split heuristic thresholds (66/70) — all adjust after real use.
- Gist/open-loop prompt wording — iterate on label quality live.
- `THROTTLE_TURNS = 6` — unchanged, still tunable.
