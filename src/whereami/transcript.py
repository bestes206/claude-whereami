import json
import re
from typing import List, Optional

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
