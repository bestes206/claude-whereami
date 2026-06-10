# src/whereami/statusline.py
import json
import math
import os
import shutil
import sys
import textwrap
import time
from typing import List, Optional

from . import cache, transcript


_RESET = "\033[0m"
_GREEN = "\033[32m"
_AMBER = "\033[33m"
_RED = "\033[31m"
_DIM = "\033[90m"  # bright-black / grey: no data yet

LINE2_LIMIT = 150  # line-2 head-keep ceiling (constant by design: editable install)

PEEK_SECONDS = 30        # peek window; mtime aging out is the auto-collapse
PEEK_MSG_LIMIT = 600     # peek message hard ceiling (chars)
PEEK_WRAP_MAX = 100      # wrap at min(terminal columns, this)
PEEK_MSG_MAX_LINES = 4   # rendered-line cap for the peek message
GOAL_PAREN_LIMIT = 40    # opening_goal head-truncation in the parenthetical
SPLIT_SCORE = 85         # split? when score alone exceeds this
SPLIT_SCORE_WITH_CTX = 66  # …or score exceeds this AND context-% exceeds:
SPLIT_CTX_PCT = 70


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def fmt_duration(ms: int) -> str:
    secs = max(0, int(ms // 1000))  # clamp clock skew, as fmt_ago does
    if secs < 60:
        return "{}s".format(secs)
    mins = secs // 60
    if mins < 60:
        return "{}m".format(mins)
    return "{}h{}m".format(mins // 60, mins % 60)


def context_pct(data: dict) -> Optional[int]:
    """Return context-window fill % from the harness payload, or None if absent.
    Prefers the pre-computed `context_window.used_percentage`; otherwise derives
    it from the input/cache token counts vs the window size. We read only the
    payload — never reverse-engineer it from the transcript."""
    cw = data.get("context_window")
    if not isinstance(cw, dict):
        return None
    val = cw.get("used_percentage")
    if isinstance(val, (int, float)):
        return int(round(val))
    size = cw.get("context_window_size")
    usage = cw.get("current_usage")
    used = 0
    if isinstance(usage, dict):
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            tok = usage.get(key)
            if isinstance(tok, (int, float)):
                used += tok
    if not used:
        total_in = cw.get("total_input_tokens")
        if isinstance(total_in, (int, float)):
            used = total_in
    if used and isinstance(size, (int, float)) and size > 0:
        return int(round(used / size * 100))
    return None


def _color_for(score: int) -> str:
    if score <= 33:
        return _GREEN
    if score <= 66:
        return _AMBER
    return _RED


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _clean(value) -> str:
    """Trust nothing in the cache: model-authored or hand-edited fields must
    be strings, newline-free (ANSI resets are per-line), and non-blank — a
    malformed minor field degrades alone, never the whole panel."""
    if not isinstance(value, str):
        return ""
    return _collapse(value)


def _score_value(cached: dict) -> Optional[int]:
    """The cache score as an int, or None if it isn't a real finite number —
    bool subclasses int and must not pass as a score."""
    score = cached.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    if not math.isfinite(score):
        return None
    return int(score)


def gist_segment(cached: dict) -> str:
    """The gist in colored words — a written message, not a colored glyph.
    Missing score OR gist (brand-new session, v1 cache) → dim placeholder."""
    score = _score_value(cached)
    gist = _clean(cached.get("gist"))
    if score is None or not gist:
        return _DIM + "…" + _RESET
    return _color_for(score) + gist + _RESET


def _gauges(data: dict) -> List[str]:
    segs = []
    cost = data.get("cost") or {}
    dur = cost.get("total_duration_ms")
    if dur:
        segs.append("⏱ " + fmt_duration(dur))
    pct = context_pct(data)
    if pct is not None:
        # ⊠ (squared-times) glyph reads as context-window fill; mono, line-weight
        segs.append("⊠ {}%".format(pct))
    if os.environ.get("WHEREAMI_SHOW_COST"):
        usd = cost.get("total_cost_usd")
        if usd:
            segs.append("\U0001f4b2{:.2f}".format(usd))
    return segs


def render_normal(data: dict, cached: dict, last: Optional[str]) -> str:
    lines = [" · ".join([gist_segment(cached)] + _gauges(data))]
    if last:
        lines.append("❯ " + truncate(_collapse(last), LINE2_LIMIT))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Peek helpers
# ---------------------------------------------------------------------------

def peek_active(now: float) -> bool:
    try:
        mtime = os.stat(str(cache.peek_path())).st_mtime
    except OSError:
        return False
    age = now - mtime
    return 0 <= age < PEEK_SECONDS   # lower clamp: a future-dated mtime is inert


def fmt_ago(secs: float) -> str:
    secs = max(0, int(secs))
    if secs < 60:
        return "{}s".format(secs)
    mins = secs // 60
    if mins < 60:
        return "{}m".format(mins)
    hours = mins // 60
    if hours < 24:
        return "{}h".format(hours)
    return "{}d".format(hours // 24)


def staleness_segment(cached: dict, now: float) -> str:
    """Staleness in TIME, never turns: on a resumed session the turn-delta is
    0 while the data is days old — a turn display would claim max freshness."""
    ts = cache.ts_to_epoch(cached.get("ts"))
    if ts is None:
        return "no score yet"
    return "scored {} ago".format(fmt_ago(now - ts))


def failure_segment(cached: dict, now: float) -> Optional[str]:
    fail = cache.ts_to_epoch(cached.get("last_failure_ts"))
    if fail is None:
        return None
    ts = cache.ts_to_epoch(cached.get("ts"))
    if ts is not None and ts >= fail:
        return None
    return "last compute failed {} ago".format(fmt_ago(now - fail))


def split_hint(cached: dict, ctx_pct: Optional[int]) -> bool:
    score = _score_value(cached)
    if score is None:
        return False
    if score > SPLIT_SCORE:
        return True   # fully drifted, even with low context
    return score > SPLIT_SCORE_WITH_CTX and ctx_pct is not None and ctx_pct > SPLIT_CTX_PCT


def _peek_width() -> int:
    # The renderer has no tty: get_terminal_size falls back to COLUMNS, else 80.
    return min(shutil.get_terminal_size((80, 24)).columns, PEEK_WRAP_MAX)


def wrap_message(text: str, width: int) -> List[str]:
    """Head-keep to PEEK_MSG_LIMIT, wrap, cap at PEEK_MSG_MAX_LINES; any
    truncation ends the visible text with an ellipsis."""
    collapsed = _collapse(text)
    truncated = len(collapsed) > PEEK_MSG_LIMIT
    body = collapsed[:PEEK_MSG_LIMIT]
    avail = max(10, width - 2)   # room for the "❯ " prefix / 2-space indent
    lines = textwrap.wrap(body, width=avail) or [""]
    if len(lines) > PEEK_MSG_MAX_LINES:
        lines = lines[:PEEK_MSG_MAX_LINES]
        truncated = True
    if truncated:
        last = lines[-1]
        if len(last) + 1 > avail:
            last = last[: avail - 1]
        lines[-1] = last + "…"
    out = ["❯ " + lines[0]]
    out.extend("  " + l for l in lines[1:])
    return out


def open_loop_line(cached: dict, turns: int) -> Optional[str]:
    loop = _clean(cached.get("open_loop"))
    if not loop:
        return None   # nothing awaited → no line, never a fabricated ask
    # Any turns since the score → presumptively already answered → dim
    # ("probably answered / refreshing"). A count BEHIND the cache (reset
    # .turns) is inconsistent, so equally presumptively stale.
    stale = turns != cache.turns_at_last_compute(cached)
    text = "⊙ your turn: " + loop
    return _DIM + text + _RESET if stale else text


def render_peek(data: dict, cached: dict, last: Optional[str],
                turns: int, now: float) -> str:
    score = _score_value(cached)
    gist = _clean(cached.get("gist"))
    scored = score is not None and bool(gist)
    if scored:
        head = _color_for(score) + "drift {} · {}".format(score, gist) + _RESET
        goal = truncate(_clean(cached.get("goal")) or _clean(cached.get("opening_goal")),
                        GOAL_PAREN_LIMIT)
        if goal:
            head += "  " + _DIM + "(goal: {})".format(goal) + _RESET
    else:
        head = _DIM + "…" + _RESET   # degraded: no "drift —", no empty parenthetical
    lines = [head]
    if last:
        lines.extend(wrap_message(last, _peek_width()))
    loop = open_loop_line(cached, turns)
    if loop:
        lines.append(loop)
    tail = _gauges(data) + [staleness_segment(cached, now)]
    failure = failure_segment(cached, now)
    if failure:
        tail.append(failure)
    # Only a panel that shows the score may act on it: a not-yet-scored
    # placeholder must not recommend a split from the same unusable score.
    if scored and split_hint(cached, context_pct(data)):
        tail.append("split?")
    lines.append(" · ".join(tail))
    return "\n".join(lines)


def _maybe_recompute(session_id: str, transcript_path: str,
                     cached: dict, turns: int) -> None:
    """The renderer's only write path (via the shared spawn guard). Entirely
    exception-swallowed: rendering must never break."""
    try:
        from . import drift   # lazy: keep normal-mode startup fast
        if drift.peek_due(cached, turns):
            drift.maybe_spawn_compute(session_id, transcript_path)
    except BaseException:
        pass


def render(data: dict, now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    session_id = data.get("session_id", "")
    transcript_path = data.get("transcript_path", "")
    cached = cache.load_cache(session_id)
    last = transcript.last_human_text(transcript_path) if transcript_path else None
    if peek_active(now):
        turns = cache.load_turns(session_id)
        if session_id and transcript_path:
            _maybe_recompute(session_id, transcript_path, cached, turns)
        return render_peek(data, cached, last, turns, now)
    return render_normal(data, cached, last)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        print("")
        return
    try:
        print(render(data))
    except Exception:
        # Rendering must never break the statusline: a hostile payload or
        # corrupted cache degrades to a blank line, never a visible error.
        print("")
