import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

CACHE_DIR = Path(os.path.expanduser("~/.claude/whereami"))


def _safe(session_id: str) -> str:
    return session_id.replace("/", "_")


def _path(session_id: str) -> Path:
    return CACHE_DIR / (_safe(session_id) + ".json")


def turns_path(session_id: str) -> Path:
    return CACHE_DIR / (_safe(session_id) + ".turns")


def marker_path(session_id: str) -> Path:
    return CACHE_DIR / (_safe(session_id) + ".computing")


def peek_path() -> Path:
    return CACHE_DIR / "peek"


def ts_to_epoch(value) -> Optional[float]:
    """Parse a cache iso-8601 timestamp to epoch seconds; None if absent/garbage."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def failure_epoch(data: Dict) -> Optional[float]:
    """Epoch of an UNHEALED parse failure: last_failure_ts when it is newer
    than ts, else None (a success at or after the failure clears it). The
    single definition behind both the spawn backoff and the peek failure
    badge — the badge is the backoff's only observability, so the two must
    never diverge."""
    fail = ts_to_epoch(data.get("last_failure_ts"))
    if fail is None:
        return None
    ts = ts_to_epoch(data.get("ts"))
    if ts is not None and ts >= fail:
        return None
    return fail


def turns_at_last_compute(data: Dict) -> int:
    """The cache's turns_at_last_compute as an int; the file is hand-editable,
    so a missing/garbage/bool counter reads as 0 — degrade, never raise."""
    value = data.get("turns_at_last_compute")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _atomic_write(target: Path, text: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Unique temp file per writer: concurrent Stop hooks / a detached compute
    # can write the same session at once, and a shared temp name would make
    # os.replace fail for the loser. mkstemp guarantees a private name; the
    # os.replace onto the final path stays atomic (last write wins).
    fd, tmp = tempfile.mkstemp(dir=str(CACHE_DIR), prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_cache(session_id: str) -> Dict:
    try:
        with open(_path(session_id), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    # The spec sanctions hand-editing this file (goal escape hatch); a non-dict
    # must degrade to "no cache", never raise out of the hook or renderer.
    return data if isinstance(data, dict) else {}


def save_cache(session_id: str, data: Dict) -> None:
    _atomic_write(_path(session_id), json.dumps(data))


def load_turns(session_id: str) -> int:
    """Hook-owned bare-integer turn count; missing or unparseable reads as 0."""
    try:
        return int(turns_path(session_id).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def save_turns(session_id: str, count: int) -> None:
    # Atomic replace: a lost increment under a rare hook-vs-hook race is
    # benign throttle skew, but a torn read would not be.
    _atomic_write(turns_path(session_id), str(count))


def increment_turns(session_id: str) -> int:
    count = load_turns(session_id) + 1
    save_turns(session_id, count)
    return count
