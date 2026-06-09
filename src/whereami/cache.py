import json
import os
import tempfile
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
    target = _path(session_id)
    # Unique temp file per writer: concurrent Stop hooks / a detached compute
    # can write the same session at once, and a shared temp name would make
    # os.replace fail for the loser. mkstemp guarantees a private name; the
    # os.replace onto the final path stays atomic (last write wins).
    fd, tmp = tempfile.mkstemp(dir=str(CACHE_DIR), prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
