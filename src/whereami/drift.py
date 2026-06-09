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
