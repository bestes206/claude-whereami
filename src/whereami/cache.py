import json
import os
from pathlib import Path
from typing import Dict

CACHE_DIR = Path(os.path.expanduser("~/.claude/whereami"))


def _path(session_id: str) -> Path:
    safe = session_id.replace("/", "_")
    return CACHE_DIR / (safe + ".json")


def load_cache(session_id: str) -> Dict:
    try:
        with open(_path(session_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cache(session_id: str, data: Dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(session_id).with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, _path(session_id))
