# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-06-10

The orientation panel. The single throttled Haiku call stops being a
drift-scorer and becomes an orienter: it now produces a *gist* (what the
session is doing right now), an *open loop* (what the agent is waiting on
from you), and a distilled *goal*, alongside the drift score — rendered as a
two-line statusline always, and an expanded panel on demand.

### Added

- **Two-line statusline**: the session gist in drift-colored words
  (green/amber/red), elapsed time, context %, plus the live last human
  message on its own line.
- **Peek mode**: touching `~/.claude/whereami/peek` (bind it to a hotkey)
  expands the statusline into a full orientation panel for ~30 s — drift
  score, gist, goal, the last message near-full, the open loop
  (`⊙ your turn: …`), time-based staleness (`scored 4m ago`), and a
  `split?` hint when drift and context pressure say to start fresh.
- **Open-loop signal**: the sidecar now reads the tail of the last assistant
  message, so the panel can say what the agent is waiting on.
- **One guarded spawn path** shared by the Stop hook and the renderer:
  atomic marker create, rename-reclaim of stale markers, and a failure
  backoff that caps a persistently broken CLI at ~6 retries/hour.
- **Two-file cache with a single writer each** (`<sid>.json` compute-owned,
  `<sid>.turns` hook-owned) — eliminates every cross-writer lost update
  without locking.
- Peek-time recompute trigger, so a stale panel self-corrects while open.
- `refreshInterval` statusline wiring, so the panel collapses on its own
  while the session is idle.

### Changed

- Parse failures now write only a `last_failure_ts` and preserve the last
  good orientation (v1 rendered a failure as a green blank); failures are
  visible in the peek panel and drive the retry backoff.
- The distilled `goal` is keep-first: written once, never overwritten, so a
  hand-edit to the cache file sticks — even one landing mid-compute.
- The `/whereami` skill reads the v2 cache fields
  (`score`/`gist`/`open_loop`/`goal`).

### Fixed

- Post-ship hardening (~15 fixes): the renderer never raises; ANSI and
  control bytes are stripped from all rendered text; bool and non-finite
  scores are rejected as parse failures or cache garbage; a shape-changed
  CLI envelope degrades instead of raising; corrupt counters read as 0;
  negative durations clamp to 0 s; a future-dated failure timestamp can't
  freeze spawning; a turn count behind the cache reads as due-and-stale;
  no split hint on a not-yet-scored panel; the hook exits 0 on malformed
  payloads.

## [0.1.0] - 2026-06-09

Initial release: the drift dot.

### Added

- **Drift sidecar**: a Stop hook counts turns and, every 6 turns, spawns a
  detached compute that scores session drift via the logged-in `claude` CLI
  (Haiku, on your subscription — no API key).
- **Statusline renderer**: ANSI-colored coherence dot, the last genuine
  human message (truncated), elapsed time, context %, and an optional cost
  segment behind `WHEREAMI_SHOW_COST`.
- Per-session cache with atomic writes under `~/.claude/whereami/`.
- Transcript readers: tail-from-end, head, and genuine-human-turn
  extraction from Claude Code transcript JSONL.
- Read-only `/whereami` skill for an in-context deep view.

[0.2.0]: https://github.com/bestes206/claude-whereami/releases/tag/v0.2.0
[0.1.0]: https://github.com/bestes206/claude-whereami/commits/main
