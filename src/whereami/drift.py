import json
import re
import subprocess
from typing import Callable, Dict, Optional, Tuple

HAIKU_MODEL = "claude-haiku-4-5-20251001"
CLI_TIMEOUT = 60  # seconds: claude -p subprocess timeout

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
            capture_output=True, text=True, timeout=CLI_TIMEOUT,
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


_ORIENT_RULES = """\
Rules:
- "gist": 3-8 words naming the SPECIFIC artifact or feature being worked on. \
Generic words like "code", "changes", "working", "fixing" are forbidden. \
Describe what the USER is trying to accomplish, not the agent's busywork.
- "open_loop": one short line stating what the agent is waiting on from the user. \
It MUST be "" when the end of the assistant's last message is not waiting on \
anything — never invent an ask.
- "score": integer 0-100. 0 = recent activity is fully on the original goal, \
100 = a completely different topic."""

_GOAL_RULE = '- "goal": at most 8 words restating the original goal.'

_CONTRACT_WITH_GOAL = ('{"score": <int>, "gist": "<words>", '
                       '"open_loop": "<line or empty>", "goal": "<words>"}')
_CONTRACT = '{"score": <int>, "gist": "<words>", "open_loop": "<line or empty>"}'


def build_prompt(opening_goal: str, recent: str, assistant_tail: str,
                 prev_gist: Optional[str], want_goal: bool) -> str:
    parts = [
        "You orient a developer returning to a coding session. Compare the "
        "session's ORIGINAL GOAL to its RECENT activity.",
        "ORIGINAL GOAL (the user's first real messages):\n" + opening_goal,
        "RECENT ACTIVITY (the user's latest messages, oldest first):\n" + recent,
    ]
    if assistant_tail:
        parts.append("END OF THE ASSISTANT'S LAST MESSAGE (it may be waiting on "
                     "the user):\n" + assistant_tail)
    rules = _ORIENT_RULES
    if want_goal:
        # Requested only until a goal is cached — re-asking is token waste.
        rules += "\n" + _GOAL_RULE
    if prev_gist:
        rules += ('\n- Previous gist: "{}" — keep it unless the focus genuinely '
                  "changed.".format(prev_gist))
    parts.append(rules)
    contract = _CONTRACT_WITH_GOAL if want_goal else _CONTRACT
    parts.append("Reply with ONLY this JSON object, no prose:\n" + contract)
    return "\n\n".join(parts)


def parse_orientation(text: str, want_goal: bool) -> Optional[Dict]:
    """Field-tolerant parse: accepted iff score and gist validate; bad
    open_loop coerced to ""; missing goal ignored. None only on score/gist
    failure — one malformed minor field must not discard a good score+gist."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        score = int(data.get("score"))
    except (TypeError, ValueError):
        return None
    gist = data.get("gist")
    if not isinstance(gist, str) or not gist.strip():
        return None
    out = {"score": max(0, min(100, score)), "gist": gist.strip()}
    open_loop = data.get("open_loop")
    out["open_loop"] = open_loop.strip() if isinstance(open_loop, str) else ""
    if want_goal:
        goal = data.get("goal")
        if isinstance(goal, str) and goal.strip():
            out["goal"] = goal.strip()
    return out


def orient(opening_goal: str, recent: str, assistant_tail: str,
           prev_gist: Optional[str], want_goal: bool,
           runner: Optional[Callable[[str], str]] = None) -> Optional[Dict]:
    run = runner if runner is not None else _run_claude
    reply = run(build_prompt(opening_goal, recent, assistant_tail, prev_gist, want_goal))
    return parse_orientation(reply, want_goal)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--compute":
        compute(sys.argv[2], sys.argv[3])
    else:
        run_hook()
