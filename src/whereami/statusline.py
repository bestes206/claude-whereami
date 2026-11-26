# src/whereami/statusline.py
import json
import math
import os
import re
import sys
from typing import List, Optional

from . import cache, transcript


_RESET = "\033[0m"
_GREEN = "\033[32m"
_AMBER = "\033[33m"
_RED = "\033[31m"
_DIM = "\033[90m"  # bright-black / grey: no data yet

LINE2_LIMIT = 150  # line-2 head-keep ceiling (constant by design: editable install)


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


# Terminal-active bytes in rendered text: whole CSI/OSC sequences (pasted
# shell logs, prompt-injected model fields via JSON \u escapes), then any
# bare control byte. Whitespace-class bytes (\t\n\v\f\r, \x1c-\x1f, \x85)
# are deliberately NOT in the class — _collapse turns those into spaces.
_CTRL_RE = re.compile(
    "\x1b\\[[0-?]*[ -/]*[@-~]"                  # CSI: colors, cursor movement
    "|\x1b\\][^\x07\x1b]*(?:\x07|\x1b\\\\)?"    # OSC: window title, hyperlinks
    "|[\x00-\x08\x0e-\x1b\x7f-\x84\x86-\x9f]"   # other controls incl. bare ESC
)


def _collapse(text: str) -> str:
    return " ".join(_CTRL_RE.sub("", text).split())


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


def render(data: dict) -> str:
    session_id = data.get("session_id", "")
    transcript_path = data.get("transcript_path", "")
    cached = cache.load_cache(session_id)
    last = transcript.last_human_text(transcript_path) if transcript_path else None
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
