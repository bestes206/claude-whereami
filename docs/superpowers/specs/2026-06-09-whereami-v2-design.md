# whereami v2 — Orientation Panel — Design Spec

**Date:** 2026-06-09
**Status:** Approved design, revised after a three-lens independent review
(platform-facts, concurrency, product/UX) and a two-agent validation round,
ready for implementation planning
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
  own row. Docs warn multi-line + escape codes are more rendering-fragile
  than single-line; behavior when the **line count changes between renders**
  is undocumented → manual flicker test required (risk C).
- **No hover anywhere**; ANSI passes through; OSC 8 hyperlinks pass through
  and are Cmd+clickable (Ghostty supports them). Deferred, not used in v2.
- **`refreshInterval`** (exact field name) re-runs the statusline command on
  a fixed timer in addition to event-driven renders, **integer seconds,
  minimum 1**, and — verified — keeps firing while the session is idle.
  Event renders: each assistant message, `/compact`, permission-mode change;
  debounced 300ms.
- Payload fields used (`session_id`, `transcript_path`,
  `cost.total_duration_ms`, `context_window.used_percentage` /
  `current_usage` / `context_window_size`) all verified against the current
  schema with those exact names.
- **Two undocumented contracts we knowingly rely on** (risk E): the
  `claude -p --output-format json` envelope's `result` field (v1 ships on
  it; not in official docs), and the presumption that subscription `-p`
  calls draw on the Pro/Max rate-limit windows (inferred, not documented).
- Checked recent release notes (through 2026-w22): no native feature
  (Agent view, `/goal`, `/usage` breakdowns) covers in-session semantic
  orientation; the gap this tool fills still exists.

## Core idea

**Precompute the whole orientation panel in the sidecar; render it two ways.**
The single throttled Haiku call stops being a drift-scorer and becomes an
orienter: it produces a *gist* ("what is this session doing right now"), an
*open loop* ("what is the agent waiting on from me"), a distilled *goal*, and
the drift score. The statusline renders a compact two-line view always, and
an expanded panel on demand ("peek") — both read-only, instant-feeling,
network-free, zero context cost. `/whereami` survives unchanged in role (the
model's own in-context read) but stops being the daily driver, which is what
makes `Esc Esc` graceful: you mostly stop needing it.

## Components (deltas from v1)

### 1. Drift sidecar → orientation sidecar (`drift.py`)

Trigger and cadence are unchanged (Stop hook, every `THROTTLE_TURNS = 6`
turns, detached spawn, logged-in `claude` CLI, Haiku, no API key) — but the
hook's *internals* change; see §5.

**Prompt inputs:**
- opening goal (cached, else `opening_turns`)
- recent genuine human turns (last ~4, as v1)
- **new:** the **last ~700 chars** of the last assistant message
  (`transcript.last_assistant_text()`) — *tail*, not head: the ask lives at
  the end of assistant messages, and head-truncation would delete exactly
  the signal the open-loop field exists to capture
- **new:** the previous gist, for continuity (see prompt hardening)

**Model contract** — reply is exactly one JSON object:

```json
{"score": 0-100,
 "gist": "3-8 words: what the session is doing right now",
 "open_loop": "one line: what the agent awaits from the user, or \"\" if nothing",
 "goal": "<= 8 words restating the original goal"}
```

The `goal` field is requested **only until a goal is cached** (keep-first,
below); thereafter the prompt and contract are the 3-field object —
re-asking would be pure token waste and failure surface.

**Field-level tolerance (not all-or-nothing):** a reply is accepted iff
`score` and `gist` validate; an invalid or missing `open_loop` is coerced to
`""`; a missing `goal` is ignored. Only score/gist failure counts as a parse
failure — one malformed minor field must not discard a good score+gist.

**Prompt hardening** (the gist is the new primary always-on signal; these are
its quality mitigations, all prompt-side and free):
- name the specific artifact/feature being worked on; generic words like
  "code", "changes", "working" are forbidden
- describe what the **user** is trying to accomplish, not the agent's
  busywork
- continuity: "previous gist: {gist} — keep it unless the focus genuinely
  changed" (damps gist whiplash between amnesiac computes)
- `open_loop` MUST be `""` when the last assistant message isn't waiting on
  anything — no fabricated asks

**Parse-failure semantics (changed from v1):** on parse failure the compute
writes **only** a `last_failure_ts` field into `<sid>.json`, preserving all
other fields — the last good data persists and honestly ages. (v1's behavior
— write score 0 + empty label — would render a parse failure as a green
blank: the best-case signal dressed over a failure, destroying the previous
good gist.) `last_failure_ts` makes failure *observable* (§4 surfaces it in
peek) and drives the retry backoff (§5) so a persistent break — deprecated
model id, changed envelope — degrades to a few retries per hour, not one
wasted call per turn.

**`goal` is keep-first:** written on the first successful compute, never
overwritten (its input, the opening turns, is constant; rewriting it would
only add model variance). Escape hatch for a bad first goal (e.g., a session
whose opening turn was "reply ok"): hand-edit the `goal` field in
`<sid>.json` — it is never overwritten, so the edit sticks. Deleting the
cache file does *not* fix it (the goal would regenerate from the same
opening turns). The `label` field dies; `gist` replaces it.

### 2. Cache — two files, single writer each (`cache.py`)

```
~/.claude/whereami/<sid>.json    compute-owned:
  { "score": 58, "gist": "CI retry backoff logic",
    "open_loop": "choose a backoff strategy", "goal": "in-session reorientation tool",
    "ts": "iso-8601", "opening_goal": "first human turns, joined",
    "turns_at_last_compute": 12 }

~/.claude/whereami/<sid>.turns   hook-owned: a bare integer turn count
```

`<sid>.json` may additionally carry `last_failure_ts` (iso-8601), written by
the compute on parse failure (§1).

**Why the split:** v1 had both the Stop hook and the compute child doing
whole-dict read-modify-write on one file. Writes are atomic
(mkstemp + `os.replace`) but RMW is not transactional — and the compute
holds its loaded dict across a ≤60s LLM call, so its save could revert
`turns_seen` increments that landed meanwhile (stretching the throttle and
falsifying staleness). Single-owner files eliminate every cross-writer lost
update with no locking.

- The hook writes `.turns` atomically (mkstemp + `os.replace`, as for
  `.json`); any reader treats a missing or unparseable `.turns` as 0. The
  increment itself is RMW — a lost increment under a rare hook-vs-hook race
  is benign throttle skew, but a torn read would not be, hence the atomic
  replace.
- The compute reads `.turns` **once at compute start, before reading the
  transcript**, so `turns_at_last_compute` never claims turns the gist does
  not reflect. Turns landing during the LLM call leave the renderer's
  staleness trigger true → self-correcting (one extra guard-absorbed
  compute) rather than self-suppressing.
- The renderer reads both files (read-only; `os.replace` guarantees readers
  never see torn JSON).

**v1→v2 transition:** a resumed session may have a v1 cache with `score` but
no `gist`. The renderer treats a missing `gist` as not-yet-scored (dim
placeholder, never an empty colored void), and a missing `gist` is a
recompute trigger on **both** paths (§5) — the renderer arm self-heals on
peek, the hook arm self-heals within a turn (necessary because the v1 cache's
large `turns_at_last_compute` against a fresh `.turns` count of ~1 makes the
turn-delta arm negative for many turns).

### 3. Statusline, normal mode — two lines (`statusline.py`)

```
parser retry logic · ⏱ 42m · ⊠ 63%
❯ ok now let's make the retry logic exponential with jitter and cap it at 5
```

- **Line 1:** the gist in **colored words** — green `0–33`, amber `34–66`,
  red `67–100` (same thresholds, still tunable). No dot. When score *or*
  gist is missing: a dim `…` placeholder. Then the existing gauges: `⏱`
  elapsed, `⊠` context-%, env-gated cost (all unchanged; `WHEREAMI_SHOW_COST`
  survives from v1).
- **Line 2:** `❯ ` + last genuine human message: newlines collapsed, **head
  kept**, ellipsis at ~150 chars (a constant in source — this is an editable
  install; no env knob). Head-keep suits imperative-first messages;
  paste-then-ask messages losing their tail is an accepted limitation.
  Line omitted entirely when there is no last message — render degrades to
  one line.
- Reads = cache files + transcript tail + one peek-file stat. The renderer's
  only write path is the spawn guard (§5), and that entire path is
  exception-swallowed: rendering must never break.

### 4. Peek mode — the hover replacement

- **Trigger:** touching `~/.claude/whereami/peek`. No console script — a
  packaged `touch` is heavier than the touch itself. The install step ships
  the one-liner (`mkdir -p ~/.claude/whereami && touch ~/.claude/whereami/peek`)
  plus a paste-ready Raycast/Hammerspoon snippet, so hotkey wiring is a
  30-second copy, not a project (the headline feature must not gate on DIY
  research).
- **Active while** `0 ≤ now − mtime < PEEK_SECONDS (30)` — the lower clamp
  makes a future-dated mtime (clock step) inert. Re-pressing refreshes the
  mtime and intentionally extends the window ("hold it open by tapping");
  there is deliberately no early dismiss. Auto-collapse is the mtime aging
  out; the peek file is global by design (a hotkey can't know which session
  is focused; expanding every session's statusline is harmless since only
  the focused window is visible).
- **Panel appears within one refresh tick (≤ `refreshInterval`)** — not
  literally instantly.

**Expanded panel** (replaces the normal two lines while active):

```
drift 58 · CI retry backoff logic   (goal: in-session reorientation tool)
❯ ok now let's make the retry logic exponential with jitter and cap it
  at 5 attempts, and while you're in there rename the helper
⊙ your turn: choose a backoff strategy
⏱ 42m · ⊠ 63% · scored 4m ago
```

- Line 1 colored as normal mode. Goal parenthetical = the cached distilled
  `goal`, else `opening_goal` head-truncated to ~40 chars, else omitted.
- **Line 2 shows the last human message in (near-)full:** head-kept to a
  hard ceiling of ~600 chars, wrapped at `min(terminal columns, 100)` — the
  renderer has no tty, so `shutil.get_terminal_size()` falls back to
  `COLUMNS`/80 — capped at 4 rendered lines; any truncation ends the visible
  text with `…`. Peek is transient and multi-line, so this verbosity is
  free; it (not a bigger one-line number) is what closes pain point 1.
- `⊙` line: omitted when `open_loop` is empty/absent; rendered **dim** when
  any turns have passed since the score (a stale open loop is presumptively
  already answered — a confident stale "your turn: X" after you've done X is
  worse than none). With `THROTTLE_TURNS = 6` the `⊙` opens dim on most
  peeks (~5/6) and brightens when the peek-triggered recompute lands — dim
  reads as "probably answered / refreshing". A recompute slower than
  `PEEK_SECONDS` lands after the panel collapsed; re-tapping to hold the
  window covers it.
- **Staleness is displayed in TIME** ("scored 4m ago", "3d ago", "no score
  yet") from `ts`. Turn-delta remains the recompute *trigger* only — on a
  resumed session the turn-delta is 0 while the data is days old, so a
  turn-based display would claim maximal freshness on maximally stale data.
  When `last_failure_ts` is newer than `ts`, the segment appends
  `· last compute failed Xm ago` — a permanently broken CLI is observable on
  demand without touching normal mode's minimalism.
- **Split hint (local heuristic, no model):** when `score > 85`, or
  `score > 66` **and** context-% `> 70`, the last line appends `· split?`.
  The standalone score trigger covers fully-drifted-but-low-context sessions;
  panel-only (the always-on line stays minimal — the red gist already
  carries urgency).
- **Degraded no-cache panel** (brand-new session): line 1 collapses to the
  dim placeholder (no `drift —`, no empty goal parenthetical); `❯` line if a
  message exists; no `⊙`; gauges + "no score yet".

### 5. Spawning — one guarded path for everything

A single `maybe_spawn_compute(session_id, transcript_path)` is the **only**
way a compute gets spawned, used by **both** the Stop hook and the renderer.
(This also fixes a live v1 bug: the hook had no in-flight guard at all, so
quick turns at session start — `ts` not yet written — stacked concurrent
computes.)

Guard = marker file `<sanitized-sid>.computing` in the cache dir (same
`/`→`_` sanitization as the cache paths):

- **Acquire:** atomic exclusive create (`O_CREAT|O_EXCL`). On `EEXIST`:
  stat it — if its mtime is younger than `MARKER_TTL`, skip (a compute is in
  flight). If stale: **reclaim via `os.rename`** to a unique name (exactly
  one renamer wins; the loser's `ENOENT` means someone else is reclaiming —
  skip), unlink the renamed-away file, then proceed to the normal exclusive
  create. Rename-reclaim, not unlink-reclaim: two racing unlinkers could
  each delete the other's fresh marker and double-spawn.
- **Compute child:** wraps everything in `try/finally: unlink(marker)` — the
  marker is removed on success, early-return (no goal/recent), parse
  failure, and exception alike. The unlink tolerates `ENOENT` (a reclaim may
  have renamed it away).
- **Spawner:** if `Popen` raises after acquire, unlink the marker (otherwise
  one failed spawn suppresses retries for a full TTL).
- In the renderer, the entire attempt (mkdir, marker create, Popen) is
  wrapped in a bare except — rendering must never break.
- **Invariant:** `MARKER_TTL = 150s = 2 × (CLI timeout 60s + 15s spawn
  slop)` — a legitimately-running compute must never be shadowed by a stale
  reclaim. Recorded as an executable assert in the test suite because both
  numbers are listed as tunable.
- **TTL-bounded residual:** if the spawned child dies before entering
  `compute()` (interpreter/import failure), no unlink path runs and the
  marker leaks until reclaimed at TTL. Accepted.

**Failure backoff (both paths):** when `last_failure_ts` is newer than `ts`
*and* younger than `FAILURE_BACKOFF (600s)`, skip the spawn. Without this, a
persistent parse failure (which by design never advances `ts` or
`turns_at_last_compute`) would keep the due-condition true on every turn —
one wasted rate-limit-window call per turn, forever. With it: ~6 retries/hour
until the breakage is fixed, observable via the peek failure segment (§4).

**Renderer trigger:** peek active AND (`.turns` value >
`turns_at_last_compute` OR `gist` missing) AND not in failure backoff.
Panel opens with cached data instantly and self-corrects on a subsequent
tick.

**Hook due check:** (`ts` absent OR `.turns` − `turns_at_last_compute` ≥ 6
OR `gist` missing) AND not in failure backoff. The `gist`-missing arm makes
the v1→v2 transition self-heal without requiring a peek (§2).

**Cost bound, stated honestly:** one peek press costs up to **one compute
per stale session** — with K parallel sessions, up to K Haiku calls against
the rate-limit window — after which every session is caught up and further
presses are free until new turns arrive. Accepted at this scale (a handful
of sessions, sub-cent calls); a transcript-recency dampener is parked in
tuning if it ever isn't.

**Accepted residual:** for a session whose transcript yields no opening
goal, the compute early-returns (writing nothing), so a held-open peek
re-spawns **once per refresh tick** — the `finally`-unlink removes the
marker within the child's millisecond lifetime, so the marker suppresses
nothing here. Cost: one short-lived process per ~3s tick for the held window,
zero LLM calls (the early return precedes the call). Bounded by the 30s peek
window; accepted.

**Stop hook (CHANGED from v1):** increments the `.turns` file atomically,
runs the due check above, calls `maybe_spawn_compute`. It also
opportunistically sweeps `*.tmp` and `*.computing` files older than 1 day
(a live marker is at most ~75s old — the sweep can never race one; same for
mkstemp tmp files, live for milliseconds). Cache `.json` files are
**deliberately never deleted** — they are the data source for the deferred
v3 multi-session dashboard.

### 6. Wiring (delta)

- Global `statusLine` settings entry gains `"refreshInterval": 3` —
  **integer seconds** (verified units; min 1; fires while idle) so the panel
  collapses and refreshes without conversation events. **This is an edit to
  the user's global settings.json wiring — flag at install time, never
  silently.** The statusline command itself is unchanged (same console
  script), so the editable-venv install keeps working with no re-wiring.
- Install-time verification: touch the peek file, wait 35s, confirm the
  panel collapsed (proves the idle timer fires); run the renderer once under
  `time` (risk F budget); one live `claude -p` envelope check (risk E).
- `pyproject.toml`: no new console scripts.
- `.claude/skills/whereami/SKILL.md`: minor wording update so the drift
  section reads `score`/`gist`/`open_loop`/`goal` from the cache; role
  unchanged (read-only, Esc-Esc-friendly).

## Scope boundaries (YAGNI)

- **No** external `whereami` CLI and **no** `--all` multi-session dashboard
  (v3 candidates; the per-session caches this version builds are their data
  source when the time comes).
- **No** `history`/trend collection — cut in review: dead sessions' history
  is worthless to a future v3, which can start collecting when something
  renders it ("collect now to avoid migration" contradicted this spec's own
  caches-are-ephemera rationale).
- **No** `whereami-peek` console script (a documented `touch` one-liner).
- **No** new env-var knobs — truncation limits are constants in an editable
  install. `WHEREAMI_SHOW_COST` survives from v1 as a feature gate.
- **No** OSC 8 links, **no** git-branch/model segments (payload has them;
  declined again).
- **No** time-based compute throttle — compute stays turn-count + demand
  (peek) driven; the peek-file mtime is a UI timer and the failure backoff
  is an error-path brake, not cadence mechanisms.
- Haiku calls remain **only** in detached child processes — never in the
  renderer process itself.

## Implementation risks

- **(A) Gist staleness mid-burst.** The "now:" line can describe up to 6
  turns ago. Mitigations: line 2 (last message) is always live from the
  transcript tail; peek triggers a recompute; the continuity prompt damps
  whiplash. Accepted residual risk.
- **(B) The spawn guard is load-bearing.** Every lifecycle path (rename
  reclaim, racing reclaimers, Popen failure, compute early-return/exception,
  racing ticks, hook+renderer cross-path, backoff) must be tested
  explicitly — see Testing.
- **(C) Line-count changes between renders are undocumented.** The panel
  height varies (normal 1–2 lines; peek 3–7 depending on message wrap and
  the optional `⊙`); the transition may flicker or corrupt layout, and docs
  warn multi-line + escapes are fragile. Manual test at multiple terminal
  widths before accepting the design; fallback is a fixed line count
  (pad to a chosen max height — ugly, last resort).
- **(D) `refreshInterval` absent or reverted** (the global settings edit is
  manual): peek panels then persist until the next event render. Accepted
  degraded mode — documented, and the install verification catches it.
- **(E) Undocumented contracts.** The `-p` JSON envelope's `result` field
  may change shape and the rate-limit attribution of subscription `-p` calls
  is presumed, not documented. A persistent envelope break degrades to
  writes-nothing + `last_failure_ts`: aging-but-honest data, observable in
  peek, retried ~6×/hour under the failure backoff — no crash, no silent
  per-turn burn. Verify both contracts empirically at install.
- **(F) Renderer budget.** It now runs every ~3s in every open session,
  forever — interpreter startup + imports + two cache reads + transcript
  tail must stay ≤ ~50ms. Measure at install; if exceeded, raise the
  interval before optimizing.

## Testing

- **Sidecar:** tail-700 slicing of the assistant message; contract fixtures
  — junk → `None` → only `last_failure_ts` written, other fields preserved;
  invalid/missing `open_loop` coerced to `""` with score+gist accepted;
  missing `goal` ignored; score clamp; goal keep-first (second compute does
  not overwrite; prompt drops the goal field once cached); continuity prompt
  includes previous gist; persistent parse failure across turns → spawns
  suppressed by backoff (and retried after `FAILURE_BACKOFF`).
- **Guard lifecycle:** exclusive create wins exactly once across racing
  acquirers; `EEXIST` + fresh marker → skip; stale marker → rename-reclaim
  with exactly one winner across racing reclaimers; `finally`-unlink
  tolerates a missing marker; Popen-raise → marker unlinked; compute
  early-return and exception → marker unlinked; hook path goes through the
  same guard; marker-create raising inside the renderer → render still
  returns; executable assert `MARKER_TTL ≥ 2 × (CLI_TIMEOUT + slop)`.
- **Two-writer integrity:** `.turns` increments landing during a slow
  compute are preserved (compute never writes `.turns`), and — because
  `.turns` is read at compute start — such increments leave the renderer's
  staleness trigger true after the save (self-correcting, not
  self-suppressing). Torn/missing/garbage `.turns` reads as 0. `.turns`
  writes are atomic-replace.
- **Due checks:** hook due with v1-transition cache (`ts` present, `gist`
  absent, negative turn-delta) → due via the gist arm; held-peek no-goal
  session → one spawn per tick, zero LLM calls (pins the accepted residual).
- **Renderer goldens:** normal 2-line; 1-line degraded; dim placeholder
  (no cache); v1-cache transition (score present, `gist` absent → dim
  placeholder, both recompute triggers true); peek full panel; peek
  no-cache panel; dimmed stale `⊙`; omitted empty `⊙`; split hint (both
  trigger arms); time-based staleness strings ("4m ago", "3d ago", "no
  score yet"); failure segment (`last_failure_ts` > `ts` → "last compute
  failed Xm ago"); peek message at the 600-char ceiling wraps to ≤4 lines
  and ends with `…`.
- **Peek window:** active/expired via injected clock; future-mtime inert
  (lower clamp); re-press extends.
- All offline; runners stubbed as in v1. Python 3.9+ typing throughout.

## Open / deferred tuning

- `PEEK_SECONDS = 30`, line-2 limit (150), peek ceiling/wrap/cap
  (600 chars / min(cols, 100) / 4 lines), goal parenthetical (~40),
  `MARKER_TTL = 150` (honor the §5 invariant — asserted in tests),
  `FAILURE_BACKOFF = 600`, split thresholds (85 / 66+70),
  `THROTTLE_TURNS = 6`, `refreshInterval` (3–5s, budget-driven per risk F).
- Gist/open-loop/goal prompt wording — iterate on quality live.
- **Parked options, owner's call after real use:** bold-when-red as
  color-redundancy on line 1; dimming the normal-mode gist when ≥4 turns
  stale (declined for now — cyclic dimming may read as broken); a
  transcript-recency dampener on peek recompute fan-out; a split hint on
  the always-on line (declined for now — line 1 stays minimal).
