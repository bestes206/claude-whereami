import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Callable, Dict, Optional

from . import cache, transcript

HAIKU_MODEL = "claude-haiku-4-5-20251001"
CLI_TIMEOUT = 60  # seconds: claude -p subprocess timeout

THROTTLE_TURNS = 6
ASSISTANT_TAIL_CHARS = 700

SPAWN_SLOP = 15          # seconds: spawn + interpreter-startup allowance
MARKER_TTL = 150         # seconds; invariant: >= 2 * (CLI_TIMEOUT + SPAWN_SLOP)
FAILURE_BACKOFF = 600    # seconds between retries after a parse failure
SWEEP_AGE = 86400        # 1 day: opportunistic cleanup threshold


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
        if not isinstance(envelope, dict):
            return ""   # shape-changed envelope (risk E): degrade, never raise
        result = envelope.get("result", "")
        return result if isinstance(result, str) else ""
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def compute(session_id: str, transcript_path: str,
            runner: Optional[Callable[[str], str]] = None) -> None:
    # Read .turns ONCE, before the transcript: turns landing during the LLM
    # call must stay > turns_at_last_compute, so the renderer's staleness
    # trigger self-corrects instead of self-suppressing.
    turns_now = cache.load_turns(session_id)
    data = cache.load_cache(session_id)
    opening = data.get("opening_goal") or "\n".join(transcript.opening_turns(transcript_path))
    recent = "\n".join(transcript.recent_turns(transcript_path))
    if not opening or not recent:
        return
    # Tail, not head: the ask lives at the END of assistant messages.
    tail = (transcript.last_assistant_text(transcript_path) or "")[-ASSISTANT_TAIL_CHARS:]
    want_goal = not data.get("goal")
    result = orient(opening, recent, tail, data.get("gist"), want_goal, runner=runner)
    if result is None:
        # Parse failure: record ONLY the failure timestamp — the last good
        # data must persist and honestly age (never a green blank).
        data["last_failure_ts"] = _now_iso()
        cache.save_cache(session_id, data)
        return
    data["score"] = result["score"]
    data["gist"] = result["gist"]
    data["open_loop"] = result["open_loop"]
    if want_goal and "goal" in result:
        data["goal"] = result["goal"]   # keep-first: never overwritten later
    data["opening_goal"] = opening
    data["ts"] = _now_iso()
    data["turns_at_last_compute"] = turns_now
    data.pop("label", None)        # v1 field: dies in v2
    data.pop("turns_seen", None)   # v1 counter: lives in .turns now
    cache.save_cache(session_id, data)


def hook_due(data: Dict, turns: int) -> bool:
    if not data.get("ts"):
        return True
    if not data.get("gist"):
        return True   # v1→v2 transition self-heal (turn-delta may be negative)
    delta = turns - cache.turns_at_last_compute(data)
    # Negative delta = a reset/lost .turns file behind a surviving cache:
    # inconsistent state is due, not "fresh for the next 45 turns".
    return delta >= THROTTLE_TURNS or delta < 0


def peek_due(data: Dict, turns: int) -> bool:
    """The renderer's recompute trigger — kept beside hook_due so the two
    cadence policies (peek: any new turn; hook: THROTTLE_TURNS) and their
    shared arm ordering are tuned in one module."""
    if not data.get("gist"):
        return True
    return turns != cache.turns_at_last_compute(data)


def run_hook() -> None:
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        sys.exit(0)
    if not isinstance(payload, dict):
        sys.exit(0)   # null/list/str are valid JSON; degrade like bad JSON
    session_id = payload.get("session_id")
    transcript_path = payload.get("transcript_path")
    if not session_id or not transcript_path:
        sys.exit(0)
    turns = cache.increment_turns(session_id)
    if hook_due(cache.load_cache(session_id), turns):
        maybe_spawn_compute(session_id, transcript_path)
    sweep_stale_files()
    sys.exit(0)


def _spawn_compute(session_id: str, transcript_path: str) -> None:
    subprocess.Popen(
        [sys.executable, "-m", "whereami.drift", "--compute", session_id, transcript_path],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def in_failure_backoff(data: Dict, now: float) -> bool:
    """True while last_failure_ts is newer than ts AND younger than
    FAILURE_BACKOFF. Without this, a persistent parse failure (which never
    advances ts) would burn one rate-limit call per turn, forever."""
    fail = cache.failure_epoch(data)
    if fail is None:
        return False
    # Lower clamp as in peek_active: a future-dated timestamp (clock step,
    # mangled hand edit) must be inert, not a spawn freeze until it passes.
    return 0 <= (now - fail) < FAILURE_BACKOFF


def _acquire_marker(marker, now: float) -> bool:
    try:
        os.close(os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        pass
    except OSError:
        return False
    try:
        age = now - os.stat(str(marker)).st_mtime
    except OSError:
        return False          # vanished between EEXIST and stat: someone owns it
    if age < MARKER_TTL:
        return False          # a compute is in flight
    # Stale: reclaim by rename — exactly one renamer wins. Unlink-reclaim
    # could let two racers each delete the other's FRESH marker → double-spawn.
    # The .tmp suffix lets the sweep collect a leaked grave file.
    grave = str(marker) + ".reclaim.{}.tmp".format(os.getpid())
    try:
        os.rename(str(marker), grave)
    except OSError:
        return False          # ENOENT: another process is reclaiming — skip
    try:
        os.unlink(grave)
    except OSError:
        pass
    try:
        os.close(os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except OSError:
        return False


def maybe_spawn_compute(session_id: str, transcript_path: str,
                        now: Optional[float] = None,
                        spawner: Optional[Callable[[str, str], None]] = None) -> bool:
    """The ONLY path that spawns a compute — used by the Stop hook and the
    renderer. Marker file = in-flight guard. Returns True iff spawned."""
    now = time.time() if now is None else now
    if in_failure_backoff(cache.load_cache(session_id), now):
        return False
    cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    marker = cache.marker_path(session_id)
    if not _acquire_marker(marker, now):
        return False
    spawn = spawner if spawner is not None else _spawn_compute
    try:
        spawn(session_id, transcript_path)
    except BaseException:
        try:
            os.unlink(str(marker))
        except OSError:
            pass
        return False
    return True


def _compute_entry(session_id: str, transcript_path: str) -> None:
    """Detached-child entrypoint: the marker must come off on every exit —
    success, early return (no goal/recent), parse failure, or exception."""
    try:
        compute(session_id, transcript_path)
    finally:
        try:
            os.unlink(str(cache.marker_path(session_id)))
        except OSError:
            pass   # a stale-reclaim may have renamed it away


def sweep_stale_files(now: Optional[float] = None) -> None:
    """Remove day-old *.tmp / *.computing leftovers. A live marker is at most
    ~MARKER_TTL old and live tmp files exist for milliseconds — the sweep can
    never race them. Cache .json files are never deleted (v3 data source)."""
    now = time.time() if now is None else now
    try:
        entries = list(cache.CACHE_DIR.iterdir())
    except OSError:
        return
    for p in entries:
        if not (p.name.endswith(".tmp") or p.name.endswith(".computing")):
            continue
        try:
            if now - p.stat().st_mtime > SWEEP_AGE:
                p.unlink()
        except OSError:
            pass


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
    failure — one malformed minor field must not discard a good score+gist.

    The greedy regex is deliberate — it handles nested braces in string
    values; off-distribution multi-object replies fall through to the
    failure path."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    raw_score = data.get("score")
    if isinstance(raw_score, bool):
        return None   # int(True) == 1: a near-best score, not a parse success
    try:
        score = int(raw_score)
    except (TypeError, ValueError, OverflowError):
        return None   # OverflowError: json.loads accepts Infinity
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
        _compute_entry(sys.argv[2], sys.argv[3])
    else:
        run_hook()
