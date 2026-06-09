import json
import re
import subprocess
from typing import Callable, Optional, Tuple

HAIKU_MODEL = "claude-haiku-4-5-20251001"

_PROMPT = """You compare a coding session's ORIGINAL GOAL to its RECENT activity \
and rate how far the conversation has drifted from the original goal.

ORIGINAL GOAL:
{goal}

RECENT ACTIVITY:
{recent}

Reply with ONLY a JSON object, no prose:
{{"score": <integer 0-100, 0=fully on topic, 100=completely different topic>, \
"label": "<3-6 word description of the current focus>"}}"""


def _run_claude(prompt: str) -> str:
    """Call Haiku via the logged-in `claude` CLI (uses the user's subscription,
    no API key). Returns the model's text, or '' on any failure."""
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt,
             "--model", HAIKU_MODEL,
             "--output-format", "json"],
            capture_output=True, text=True, timeout=60,
        )
        envelope = json.loads(proc.stdout)
        return envelope.get("result", "") or ""
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def score_drift(goal: str, recent: str,
                runner: Optional[Callable[[str], str]] = None) -> Tuple[int, str]:
    run = runner or _run_claude
    text = run(_PROMPT.format(goal=goal, recent=recent))
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return 0, ""
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return 0, ""
    try:
        score = int(data.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))
    label = str(data.get("label", "")).strip()
    return score, label


# src/whereami/drift.py  (append)
from datetime import datetime

from . import cache, transcript


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def compute(session_id: str, transcript_path: str) -> None:
    data = cache.load_cache(session_id)
    goal = data.get("opening_goal") or "\n".join(transcript.opening_turns(transcript_path))
    recent = "\n".join(transcript.recent_turns(transcript_path))
    if not goal or not recent:
        return
    score, label = score_drift(goal, recent)
    data["score"] = score
    data["label"] = label
    data["opening_goal"] = goal
    data["ts"] = _now_iso()
    data["turns_at_last_compute"] = data.get("turns_seen", 0)
    cache.save_cache(session_id, data)


# src/whereami/drift.py  (append)
import os
import subprocess
import sys

THROTTLE_TURNS = 6


def _spawn_compute(session_id: str, transcript_path: str) -> None:
    subprocess.Popen(
        [sys.executable, "-m", "whereami.drift", "--compute", session_id, transcript_path],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run_hook() -> None:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        sys.exit(0)
    session_id = payload.get("session_id")
    transcript_path = payload.get("transcript_path")
    if not session_id or not transcript_path:
        sys.exit(0)

    data = cache.load_cache(session_id)
    data["turns_seen"] = data.get("turns_seen", 0) + 1
    cache.save_cache(session_id, data)

    due = (not data.get("ts")) or (
        data["turns_seen"] - data.get("turns_at_last_compute", 0) >= THROTTLE_TURNS
    )
    if due:
        _spawn_compute(session_id, transcript_path)
    sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--compute":
        compute(sys.argv[2], sys.argv[3])
    else:
        run_hook()
