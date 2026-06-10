# whereami token diet — design

**Date:** 2026-06-10
**Status:** approved-for-planning
**Topic:** Cut whereami's per-compute token footprint and latency without leaving the subscription.

## Problem

Each orientation compute shells out to `claude -p` on the user's Pro/Max
subscription (no API key). That call drags two hidden taxes that make the tool
"worthless with the token consumption we have right now":

1. **~29K tokens of Claude Code context** per call (full system prompt, tool
   schemas, MCP definitions, settings/CLAUDE.md/skills) — for a payload whose
   real work is ~500 tokens.
2. **Extended thinking is silently on.** The orientation task triggers
   ~3,900 thinking tokens and **~32 seconds** of latency per compute. The
   visible answer is ~50 tokens; the rest is hidden thinking that still bills
   and still counts against subscription usage limits.

Both were measured on the installed CLI (v2.1.172) on 2026-06-10.

## Goal

Keep the subscription path (`claude -p`, no API key). Drop per-call token
footprint ~30×, drop latency ~30×, preserve orientation quality, and degrade
gracefully on older CLIs that lack the stripping flags.

### Measured baseline vs. target (real numbers, this CLI)

| | Current default `claude -p` | After fixes |
|---|---|---|
| Input tokens | ~29,400 | ~870 |
| Output tokens | ~4,000 (mostly hidden thinking) | ~64 |
| Latency | ~32 s | ~1 s |
| Cost / call | ~2.6¢+ | ~0.12¢ |

> **Annotation — measured at implementation (2026-06-10, CLI 2.1.172).** The
> table above is the design-time estimate. Re-measured on the real orientation
> prompt by reading the `claude -p --output-format json` envelope (`usage`,
> `total_cost_usd`, `duration_ms`), the actuals were:
>
> | | Unstripped (before) | Stripped (after) |
> |---|---|---|
> | Context (input) tokens | ~30,260 (17,681 cache-read + 12,579 cache-create) | 1,321 |
> | Output tokens | 2,257 (mostly hidden thinking) | 33 |
> | Latency | ~23 s API / ~25 s wall | ~1 s API / ~2 s wall |
> | Cost / call (`total_cost_usd`) | $0.0382 (~3.8¢) | $0.0015 (~0.15¢) |
>
> Net ≈ 20–25× across tokens, latency, and cost (close to the ~30× goal). The
> stripped figures were stable across 5 repeats; the unstripped baseline is a
> single sample and subscription latency varies. The design estimates of ~870
> input / ~64 output / ~0.12¢ / ~1 s were optimistic: the stripped call still
> carries ~1,300 input tokens (full price, no cache), and ~1 s is the model time
> while the detached compute takes ~2 s end-to-end (subprocess + CLI startup).
> The README/CHANGELOG carry these measured numbers.

Validated on the real orientation prompt: score / gist / open_loop / goal all
correct with thinking off and the system context stripped (the orientation
prompt is fully self-contained — the Claude Code system prompt never did any
of the orientation work).

## Architecture — two phases

Phase 1 is the bulk of the win and carries no behavioral change. Phase 2 is an
additive freshness feature, affordable only because Phase 1 made calls cheap.

---

### Phase 1 — Strip the invocation + disable thinking (+ capability probe)

#### The invocation

`drift._run_claude` builds its argv from a constant `STRIP_FLAGS` and sets a
thinking-off env var on the subprocess:

```
claude -p "<prompt>" --model <haiku> --output-format json
  --system-prompt "You are a JSON-only classifier. Reply with only the requested JSON object, no prose."
  --exclude-dynamic-system-prompt-sections
  --strict-mcp-config
  --setting-sources ""
  --tools ""
```

with `MAX_THINKING_TOKENS=0` in the subprocess environment.

- `--tools ""` is load-bearing: `--disallowedTools` blocks tool *use* but keeps
  ~11K of tool *schemas* in context; `--tools ""` drops the definitions.
- `MAX_THINKING_TOKENS=0` killed thinking cleanly (64 output tokens, 1 s).
  `DISABLE_INTERLEAVED_THINKING=1` did **not** (still 2,025 output tokens, 18 s)
  — it only disables interleaving, not the budget. Do not use it.
- The reply may still arrive fenced in ```json; the existing greedy-regex parser
  (`parse_orientation`) already handles fences. No parser change needed.
- **Empty-string args (`--setting-sources ""`, `--tools ""`) are version-brittle**
  and the fallback is **all-or-nothing**: if any single one of the six flags is
  rejected by a given CLI, the whole stripped call fails and the probe classifies
  the CLI as unsupported, forfeiting the optimization even on a CLI that supports
  five of six. Acceptable as a coarse v1 fallback (confirmed working on 2.1.172);
  tiered per-flag probing is a possible future refinement, not v1 scope.

#### Capability probe + fallback (older CLIs)

The flags require a recent CLI. whereami is published, so older-CLI users must
degrade, not silently stop scoring.

- New capability cache file in `CACHE_DIR` (`capabilities.json`), keyed by the
  output of `claude --version`: `{"cli_version": "<x>", "stripped_ok": <bool>}`.
- `_stripped_supported()`:
  - Read the caps cache. If `cli_version` matches the current `claude --version`
    and `stripped_ok` is known, return it (no probe).
  - On miss (no cache, or version changed), **probe**:
    1. Run a *stripped* probe call using a **small reasoning prompt** (not
       `{"ok": true}` — see below), then check the envelope for **both**:
       parseable expected JSON **and** low `output_tokens` (e.g. < 300) / short
       duration. Both pass → `stripped_ok = true`; cache by version; use stripped.
    2. If the stripped probe fails, run a trivial *unstripped* call to
       disambiguate:
       - unstripped works but stripped failed → flags unsupported →
         `stripped_ok = false`; cache by version; use unstripped.
       - unstripped also fails → transient/broken CLI → record a
         `probed_at` timestamp and **back off** (reuse `FAILURE_BACKOFF`, 600 s):
         use unstripped this compute, and do **not** re-probe until the cooldown
         elapses. Without this, a persistently-broken CLI (logged-out,
         rate-limited, offline) would fire two extra trivial calls *every*
         compute — turning a degraded state into 3× call volume. The probe runs
         inside the compute child and is otherwise outside `in_failure_backoff`'s
         guard, so it needs its own cooldown.
  - **The probe must NOT reuse `parse_orientation`** — that parser requires a
    valid `score`+`gist` and returns `None` otherwise, so it would reject a
    `{"ok": ...}` reply and mis-classify every CLI as unsupported. The probe uses
    its own `json.loads` check.
  - **Why a reasoning prompt, not `{"ok": true}`:** a trivial probe reply is tiny
    *whether or not* thinking is on, so it can't verify the thinking-off lever.
    A small prompt that *would* think if thinking were on, checked for low
    `output_tokens`, validates the flags **and** confirms `MAX_THINKING_TOKENS=0`
    actually took effect in one shot. If a future CLI ignores the env var,
    `output_tokens` spikes and the probe catches the regression instead of
    silently paying the thinking tax again.
- `_run_claude` chooses stripped vs. unstripped argv from `_stripped_supported()`.
  Unstripped path is exactly today's behavior (preserved verbatim).
- `install.py` warms the probe once during setup so the first real compute is
  not delayed. The lazy version-keyed cache remains the source of truth, so it
  self-heals across CLI upgrades/downgrades.

The probe runs inside the detached compute child; on cache-hit it is just a
file read. Cost: one (or two) trivial stripped calls per CLI version, once.

#### Testability seam

Factor the low-level subprocess call behind `_invoke(args, env) -> dict`
(returns the **full parsed JSON envelope** — `result` + `usage` — or `{}`).
Returning the whole envelope (not just `result`) lets the probe read
`usage.output_tokens` for the thinking-off check, and lets real computes record
output-token counts for observability. `_run_claude` and the probe both build
argv + call `_invoke`. Tests inject a fake `_invoke` (via `monkeypatch.setattr`,
matching the existing convention) to assert argv construction and probe
classification without spawning `claude`.

---

### Phase 2 — Return-from-idle trigger (the innovation)

Re-orientation has value when you've *lost* orientation — by being away. The
current trigger is a turn counter (`THROTTLE_TURNS = 6`), which fires during
active back-and-forth when you don't need it and not the moment you return.

Add an **idle-return** trigger so the gist is fresh the instant you come back,
on top of (not replacing) the periodic cadence. Because Phase 1 made calls
~0.12¢ / ~1 s, this is a *freshness* feature, not a cost feature, and it keeps
live drift detection intact.

**Honest cost bound:** Phase 2 *adds* computes (one per return) that the turn
counter alone wouldn't fire, so it does not strictly minimize call count. The
honest claim: per-call cost dropped ~20×, so total spend still falls far below
the pre-diet baseline — but a break-heavy user with many short sessions sees the
*smallest* net win, and in the limit could fire more computes than the bare
6-turn cadence would have (each still ~0.12¢). `WHEREAMI_IDLE_MIN` is the user's
control; a per-session idle-fire cap is a possible future guard if observed cost
ever matters.

#### Mechanism (zero extra tokens)

Every transcript entry carries an ISO-8601 `timestamp`. The idle gap is the time
between the two most recent **human** turns — i.e., how long the user was away
before opening the turn that just completed.

Two correctness traps, both of which fail *silently* and both of which pass on a
modern Python — must be handled explicitly:

- **`Z`-suffix parsing (Python 3.9/3.10).** Transcript timestamps are UTC with a
  trailing `Z` (`2026-06-10T21:42:50.649Z`). `datetime.fromisoformat` only
  accepts `Z` on Python **3.11+**; on the project's 3.9 floor it raises
  `ValueError`. The existing `cache.ts_to_epoch` swallows that error and returns
  `None`, which would route every gap to the "unparseable → idle off" path and
  silently kill the feature on 3.9/3.10 with no error surfaced. **Normalize
  before parsing**: if the value ends in `Z`, replace with `+00:00`. Put this in
  one shared helper (extend `ts_to_epoch` or a transcript-local parser) and add a
  unit test with a literal `...Z` string. (Existing cache timestamps use offset
  form, which 3.9 already parses — so this bug is *new* to transcript input and
  was never exposed.)
- **Human-turn filtering.** `type == "user"` entries are overwhelmingly
  `tool_result` envelopes, not human messages (measured ~365 tool-results : 37
  human turns in one transcript). `human_turn_timestamps` **must** reuse the
  existing `transcript.human_text(entry) is not None` inclusion test (which
  already excludes tool-results, injected context, meta, and sidechain) and pair
  each surviving entry with its `timestamp`. Filtering on `type == "user"` alone
  would pick two adjacent tool-results seconds apart and idle would never fire.

- `transcript.human_turn_timestamps(path, n=2) -> List[datetime]`: timestamps of
  the last `n` genuine human turns (via `human_text`), most-recent-first; skips
  entries with missing/garbage timestamps after `Z`-normalization.
- `drift.returned_from_idle(path, threshold_seconds) -> bool`: True iff the gap
  between the last two human turns ≥ threshold. Returns False when fewer than two
  human turns exist or timestamps are unparseable (idle trigger simply doesn't
  fire; periodic cadence still works).
- `IDLE_THRESHOLD = int(os.environ.get("WHEREAMI_IDLE_MIN", "10")) * 60`, with a
  guard that bad/negative values fall back to the 10-minute default.
- `hook_due(data, turns, idle_returned=False)` gains `... or idle_returned`.
- `run_hook` computes `idle_returned = returned_from_idle(transcript_path,
  IDLE_THRESHOLD)` and passes it in.

#### Gap semantics — accepted behavior (not a bug)

The Stop hook fires *after* the assistant finishes responding, so at hook time
the most-recent human turn is the message that **opened** the just-completed
turn. Two consequences, both deliberate:

1. **One-turn lag.** The recompute fires after your first post-return exchange
   and reflects your post-return work — so the gist is fresh the next time you
   glance, which is the goal. We accept the one-turn lag rather than add a
   second hook point.
2. **Long agent turns also trip the gap.** The human-to-human gap includes the
   previous agent turn's runtime, so an 8-minute autonomous agent run can cross
   the threshold with no human idleness. This is benign-to-desirable: a gist
   from before a long autonomous run is exactly the kind that has gone stale and
   merits a refresh. At ~0.12¢ the occasional extra recompute is noise.

The gap is large only on the *first* turn after a return, so the trigger fires
once per return, not repeatedly. Concurrency/dedup is handled by the in-flight
marker in `maybe_spawn_compute`. Note: an idle-return compute writes
`turns_at_last_compute = turns_now`, which **resets the 6-turn cadence ledger** —
benign (you just got fresh data) but non-obvious, since idle-returns and the
periodic cadence share that one counter.

`THROTTLE_TURNS` stays at 6 — the periodic cadence is now affordable and still
provides live drift detection during long unbroken sessions. The peek path
(`peek_due`, renderer recompute) is unchanged.

---

## Files touched

- `src/whereami/drift.py` — `STRIP_FLAGS`, thinking-off env, `_invoke` seam,
  `_cli_version`, probe + `_stripped_supported`, `_run_claude` rewrite;
  `IDLE_THRESHOLD`, `returned_from_idle`, `hook_due`/`run_hook` wiring.
- `src/whereami/cache.py` — `load_caps`/`save_caps` (`capabilities.json`,
  same atomic-write pattern as the session cache).
- `src/whereami/transcript.py` — `human_turn_timestamps`.
- `src/whereami/install.py` — warm the capability probe during install.
- `README.md` / `CHANGELOG.md` — update cost/latency (now ~0.12¢ / ~1 s),
  document `WHEREAMI_IDLE_MIN`, note the minimum CLI version for the stripped
  path. Version bump (0.2.0 → 0.3.0) is a release decision, flagged not assumed.

## Error handling / degradation

- Stripped probe transient failure → unstripped this compute, **back off
  `FAILURE_BACKOFF` before re-probing** (no per-turn probe storm on a broken CLI).
- Flags unsupported → unstripped permanently for that CLI version (today's
  behavior, today's cost). Silent — no nagging.
- Thinking-off failure (env var ignored by a future CLI) → caught by the probe's
  `output_tokens` check, recorded as not-fully-supported, fall back to unstripped
  rather than silently paying the thinking tax.
- Bad `WHEREAMI_IDLE_MIN` → default 10.
- Timestamp parse failures → idle trigger off; periodic cadence unaffected.
  (Note: `Z`-normalization is what *prevents* this from silently disabling idle
  on Python 3.9/3.10 — see Phase 2.)
- All existing failure paths (parse failure backoff, marker reclaim, honest
  aging) are preserved.

## Testing

- `_invoke`-injected tests: stripped vs. unstripped argv; probe classification
  (supported / unsupported / transient-with-cooldown); probe uses its own JSON
  check (not `parse_orientation`); thinking-off `output_tokens` check; caps cache
  keyed by version (hit, miss, version-change re-probe).
- **`Z`-suffix timestamp test with a literal `...Z` string** — the regression
  guard for the Python 3.9/3.10 blocker. Without it the bug passes CI on a 3.11+
  box.
- **Add `timestamp` fields to transcript fixtures** (current fixtures omit them)
  and a fixture with **interleaved `tool_result` `user` entries** so
  `human_turn_timestamps` is proven to skip tool-results, not just `type=="user"`.
- `human_turn_timestamps` / `returned_from_idle`: varied gaps, <2 human turns,
  missing/garbage timestamps, long-agent-turn-trips-gap case.
- `hook_due` with `idle_returned` True/False across existing cases (defaulted
  param keeps current direct-call tests green).
- CI already runs the 3.9/3.14 matrix (`.github/workflows/ci.yml`), so the 3.9
  floor is covered — **but only if a test exercises the `Z`-path**. The current
  fixtures omit `timestamp`, so the 3.9 job would pass green-but-dead today. The
  literal-`Z` test above is what actually arms the existing 3.9 job.
- Existing suite stays green. Run `pytest tests/ -x -q`.
- Manual: one live stripped compute, confirm gist/score quality and ~1 s latency.

## Out of scope (YAGNI)

- No local model, no embeddings, no heuristic drift scoring. The flag fixes make
  them unnecessary and they would wreck the one-command pip install.
- No change to peek rendering, drift bands, or the `/whereami` skill.
- No change to `THROTTLE_TURNS` or the periodic cadence policy.
