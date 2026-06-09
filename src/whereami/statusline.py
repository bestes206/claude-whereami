# src/whereami/statusline.py
import json
import os
import sys
from typing import Optional

from . import cache, transcript


def light_emoji(score: Optional[int]) -> str:
    if score is None:
        return "⚪"  # white circle: no data yet
    if score <= 33:
        return "\U0001f7e2"  # green
    if score <= 66:
        return "\U0001f7e1"  # amber
    return "\U0001f534"      # red


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
    """Return context-window fill % if the harness payload exposes it; else None.
    Version-dependent — we never reverse-engineer it from the transcript."""
    ctx = data.get("context")
    if isinstance(ctx, dict):
        for key in ("used_pct", "percent", "used_percent"):
            val = ctx.get(key)
            if isinstance(val, (int, float)):
                return int(val)
    return None


def render(data: dict) -> str:
    session_id = data.get("session_id", "")
    transcript_path = data.get("transcript_path", "")
    cached = cache.load_cache(session_id)

    segs = [light_emoji(cached.get("score"))]

    last = transcript.last_human_text(transcript_path) if transcript_path else None
    if last:
        segs.append('"{}"'.format(truncate(last.replace("\n", " "), 60)))

    cost = data.get("cost") or {}
    dur = cost.get("total_duration_ms")
    if dur:
        segs.append("⏱ " + fmt_duration(dur))

    pct = context_pct(data)
    if pct is not None:
        segs.append("\U0001f522 {}%".format(pct))

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
