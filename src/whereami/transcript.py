import json
import re
from datetime import datetime
from typing import Callable, List, Optional

from . import cache

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_COMMAND_RE = re.compile(r"<command-[a-z]+>.*?</command-[a-z]+>", re.DOTALL)


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


def human_text(entry: dict) -> Optional[str]:
    """Return the human-typed text of a transcript entry, or None if it is not
    a genuine human turn (assistant, tool result, injected context, meta, sidechain)."""
    if entry.get("type") != "user":
        return None
    if entry.get("isMeta") or entry.get("isSidechain"):
        return None
    msg = entry.get("message") or {}
    cleaned = _strip_injected(_content_text(msg.get("content")))
    return cleaned or None


def _parse(line: str) -> Optional[dict]:
    try:
        return json.loads(line)
    except ValueError:
        return None


def _tail_lines(path: str, max_lines: int = 200, block_size: int = 65536) -> List[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            data = b""
            while size > 0 and data.count(b"\n") <= max_lines:
                read = min(block_size, size)
                size -= read
                f.seek(size)
                data = f.read(read) + data
    except OSError:
        return []
    lines = [l for l in data.split(b"\n") if l.strip()]
    return [l.decode("utf-8", "replace") for l in lines][-max_lines:]


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
    for line in reversed(_tail_lines(path)):
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
    for line in reversed(_tail_lines(path)):
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
    tool_result envelopes) with its `timestamp`, Z-normalized. Skips turns with
    missing/garbage timestamps."""
    out = []  # type: List[datetime]
    for line in reversed(_tail_lines(path)):
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
