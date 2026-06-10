# src/whereami/statusline.py
import json
import os
import sys
from typing import Optional

from . import cache, transcript


# ANSI-colored text-width dot (U+25CF). Carries the coherence signal by color
# rather than an emoji, so it stays the same size/weight as the rest of the line.
_RESET = "\033[0m"
_GREEN = "\033[32m"
_AMBER = "\033[33m"
_RED = "\033[31m"
_DIM = "\033[90m"  # bright-black / grey: no data yet


def light(score: Optional[int]) -> str:
    if score is None:
        color = _DIM
    elif score <= 33:
        color = _GREEN
    elif score <= 66:
        color = _AMBER
    else:
        color = _RED
    return color + "●" + _RESET


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def fmt_duration(ms: int) -> str:
    secs = int(ms // 1000)
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


def render(data: dict) -> str:
    session_id = data.get("session_id", "")
    transcript_path = data.get("transcript_path", "")
    cached = cache.load_cache(session_id)

    segs = [light(cached.get("score"))]

    last = transcript.last_human_text(transcript_path) if transcript_path else None
    if last:
        segs.append('"{}"'.format(truncate(last.replace("\n", " "), 60)))

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

    return " · ".join(segs)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        print("")
        return
    print(render(data))
