import json
import re
from datetime import datetime
from typing import Callable, Iterator, List, Optional

from . import cache

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_COMMAND_RE = re.compile(r"<command-[a-z]+>.*?</command-[a-z]+>", re.DOTALL)
_BASH_INPUT_RE = re.compile(r"^<bash-input>(.*?)</bash-input>$", re.DOTALL)

# Harness-written text that arrives as a "user" entry. Transcripts from CLI
# ~2.1.33x mark the sender in `origin`; these prefixes cover older ones.
_NOT_HUMAN_PREFIXES = (
    "<task-notification>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<bash-stdout>",
    "<bash-stderr>",
)

# How far back the renderer will look for the last human message. A long
# autonomous stretch can bury it under thousands of tool-result lines.
MAX_SCAN_BYTES = 8 * 1024 * 1024


def _strip_injected(text: str) -> str:
    text = _REMINDER_RE.sub("", text)
    text = _COMMAND_RE.sub("", text)
    return text.strip()


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def _origin_kind(obj: dict) -> Optional[str]:
    origin = obj.get("origin")
    if isinstance(origin, dict):
        kind = origin.get("kind")
        return kind if isinstance(kind, str) else ""
    return None


def _clean_human(raw: str) -> Optional[str]:
    cleaned = _strip_injected(raw)
    if not cleaned or cleaned.startswith(_NOT_HUMAN_PREFIXES):
        return None
    bash = _BASH_INPUT_RE.match(cleaned)
    if bash:
        return "! " + bash.group(1).strip()
    return cleaned


def _queued_text(entry: dict) -> Optional[str]:
    """A message typed while the agent was working: stored as a queued_command
    attachment, never as a user entry."""
    att = entry.get("attachment")
    if not isinstance(att, dict) or att.get("type") != "queued_command":
        return None
    kind = _origin_kind(att)
    if kind is not None and kind != "human":
        return None
    if kind is None and att.get("humanTurn") is not True:
        return None
    return _clean_human(_content_text(att.get("prompt")))


def human_text(entry: dict) -> Optional[str]:
    """Return the human-typed text of a transcript entry, or None if it is not
    a genuine human turn (assistant, tool result, task notification, injected
    context, compact summary, meta, sidechain)."""
    if entry.get("isMeta") or entry.get("isSidechain"):
        return None
    if entry.get("type") == "attachment":
        return _queued_text(entry)
    if entry.get("type") != "user" or entry.get("isCompactSummary"):
        return None
    kind = _origin_kind(entry)
    if kind is not None and kind != "human":
        return None
    msg = entry.get("message") or {}
    return _clean_human(_content_text(msg.get("content")))


def _parse(line: str) -> Optional[dict]:
    try:
        return json.loads(line)
    except ValueError:
        return None


def _reverse_lines(path: str, max_bytes: int = MAX_SCAN_BYTES,
                   block_size: int = 65536) -> Iterator[str]:
    """Yield non-blank lines from the end of the file backwards, reading at
    most max_bytes. Stops early when the caller stops iterating."""
    try:
        f = open(path, "rb")
    except OSError:
        return
    with f:
        try:
            f.seek(0, 2)
            pos = f.tell()
            floor = max(0, pos - max_bytes)
            partial = b""
            while pos > floor:
                read = min(block_size, pos - floor)
                pos -= read
                f.seek(pos)
                chunk = f.read(read) + partial
                lines = chunk.split(b"\n")
                partial = lines.pop(0)   # may be cut mid-line: carry it back
                for raw in reversed(lines):
                    if raw.strip():
                        yield raw.decode("utf-8", "replace")
            if pos == 0 and partial.strip():
                yield partial.decode("utf-8", "replace")
        except OSError:
            return


def _head_lines(path: str, max_lines: int = 200) -> List[str]:
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    out.append(line)
                if len(out) >= max_lines:
                    break
    except OSError:
        return []
    return out


def _last_text(path: str, extract: Callable[[dict], Optional[str]]) -> Optional[str]:
    for line in _reverse_lines(path):
        entry = _parse(line)
        if entry is None:
            continue
        text = extract(entry)
        if text:
            return text
    return None


def last_human_text(path: str) -> Optional[str]:
    return _last_text(path, human_text)


def assistant_text(entry: dict) -> Optional[str]:
    """Return the text of an assistant transcript entry, or None if it is not
    a main-chain assistant turn (or has no text blocks, e.g. tool-use only)."""
    if entry.get("type") != "assistant":
        return None
    if entry.get("isMeta") or entry.get("isSidechain"):
        return None
    msg = entry.get("message") or {}
    text = _content_text(msg.get("content")).strip()
    return text or None


def last_assistant_text(path: str) -> Optional[str]:
    return _last_text(path, assistant_text)


def opening_turns(path: str, n: int = 2) -> List[str]:
    out = []
    for line in _head_lines(path):
        entry = _parse(line)
        if entry is None:
            continue
        text = human_text(entry)
        if text:
            out.append(text)
        if len(out) >= n:
            break
    return out


def recent_turns(path: str, n: int = 4) -> List[str]:
    out = []
    for line in _reverse_lines(path):
        entry = _parse(line)
        if entry is None:
            continue
        text = human_text(entry)
        if text:
            out.append(text)
        if len(out) >= n:
            break
    return list(reversed(out))


def human_turn_timestamps(path: str, n: int = 2) -> List[datetime]:
    """Timestamps of the last `n` genuine human turns, most-recent-first. Pairs
    each human turn (filtered via human_text — NOT type=='user', which is mostly
    tool_result envelopes; queued mid-turn messages count) with its
    `timestamp`, Z-normalized. Skips turns with
    missing/garbage timestamps."""
    out = []  # type: List[datetime]
    for line in _reverse_lines(path):
        entry = _parse(line)
        if entry is None:
            continue
        if human_text(entry) is None:
            continue
        ts = cache.parse_iso(entry.get("timestamp"))
        if ts is None:
            continue
        out.append(ts)
        if len(out) >= n:
            break
    return out


def is_headless(path: str) -> bool:
    """True for a `claude -p` / SDK session (entrypoint "sdk-cli") or a session
    with no transcript on disk (--no-session-persistence). Transcripts that
    predate the entrypoint field read as interactive."""
    try:
        open(path, "rb").close()
    except OSError:
        return True
    for line in _reverse_lines(path):
        entry = _parse(line)
        if entry is None:
            continue
        entrypoint = entry.get("entrypoint")
        if isinstance(entrypoint, str):
            return entrypoint == "sdk-cli"
    return False
