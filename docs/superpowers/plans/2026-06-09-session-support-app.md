# Session Support App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an in-session reorientation tool for Claude Code — a statusline coherence light + "what did I just say", backed by a throttled Haiku drift sidecar, plus a read-only `/whereami` deep-view skill.

**Architecture:** Three decoupled pieces sharing two small support modules. A **statusline renderer** reads the harness's stdin JSON payload + a cache file and prints one line (does no network I/O, fast). A **drift sidecar** runs from a `Stop` hook, increments a turn counter, and every 6th turn spawns a *detached* process that makes one Haiku call and writes the drift score to the cache. A **`/whereami` skill** is a read-only markdown skill for the on-demand rich view (self-cleaned by `Esc Esc`). Shared modules: `transcript.py` (parse JSONL → genuine human turns, strip injected harness noise, tail-from-end) and `cache.py` (per-session cache + throttle state).

**Tech Stack:** Python 3.9+ (use `Optional`/`Tuple`/`List` from `typing`, no `X | Y` unions), `anthropic` SDK (Haiku 4.5 = `claude-haiku-4-5-20251001`), `pytest`. Packaged with `pyproject.toml` console scripts; configured via `.claude/settings.json`.

**Prerequisites:**
- Python 3.9+ and `python3 -m venv` available.
- Tasks 1–9 and their tests need **no** API key — the Haiku call is mocked in every test.
- Only the **live** end-to-end checks in Task 10 (Steps 4–5) actually call Haiku, which requires `ANTHROPIC_API_KEY` set in the environment. If you don't have one at build time, implement and unit-test everything, then leave Task 10 Steps 4–5 for the user to run.
- All commands below assume the project root as the working directory and use the venv created in Task 1 (`.venv/bin/...`).

---

## File Structure

```
session-support-app/
  pyproject.toml                         # package + deps + console scripts
  src/whereami/__init__.py
  src/whereami/cache.py                  # per-session cache read/write + throttle counters
  src/whereami/transcript.py             # JSONL parsing: human turns, strip noise, tail-from-end
  src/whereami/drift.py                  # Stop-hook entry, detached compute, Haiku scoring
  src/whereami/statusline.py             # statusline renderer (reads stdin JSON + cache)
  tests/test_cache.py
  tests/test_transcript.py
  tests/test_drift.py
  tests/test_statusline.py
  .claude/skills/whereami/SKILL.md       # read-only deep-view skill
  .claude/settings.json                  # statusLine + Stop hook wiring
```

**Responsibilities (one job each):**
- `transcript.py` — the highest-risk logic (spec risks A & C): turn what Claude Code logs into "what the human actually said." Used by both the renderer and the sidecar.
- `cache.py` — owns `~/.claude/whereami/<session-id>.json`: drift result + turn counters for the message-count throttle.
- `drift.py` — orchestration + the only place that touches the network (Haiku), and the only place that must background work (spec risk B).
- `statusline.py` — pure rendering from already-available data; no network, fast (spec risk C).

**Cache schema** (`~/.claude/whereami/<session-id>.json`) — refines the spec's `msg_count` into two explicit counters for the throttle:
```json
{
  "score": 42,
  "label": "short phrase",
  "ts": "2026-06-09T10:00:00-07:00",
  "opening_goal": "first human turns, joined",
  "turns_seen": 12,
  "turns_at_last_compute": 6
}
```

**Constants:** `THROTTLE_TURNS = 6`. Light: green `0–33`, amber `34–66`, red `67–100`, no-data `⚪`.

---

## Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `src/whereami/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "whereami"
version = "0.1.0"
description = "In-session reorientation tool for Claude Code"
requires-python = ">=3.9"
dependencies = ["anthropic>=0.40"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
whereami-statusline = "whereami.statusline:main"
whereami-hook = "whereami.drift:run_hook"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]
```

- [ ] **Step 2: Create empty package files**

```bash
mkdir -p src/whereami tests
touch src/whereami/__init__.py tests/__init__.py
```

- [ ] **Step 3: Create venv and install editable + dev deps**

Run:
```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```
Expected: installs `whereami`, `anthropic`, `pytest` with no errors.

- [ ] **Step 4: Verify pytest runs (no tests yet)**

Run: `.venv/bin/pytest -q`
Expected: `no tests ran` (exit code 5) — confirms pytest is wired.

- [ ] **Step 5: Add `.gitignore` and commit**

Create `.gitignore`:
```
.venv/
__pycache__/
*.egg-info/
.pytest_cache/
```
Then:
```bash
git add pyproject.toml .gitignore src/whereami/__init__.py tests/__init__.py
git commit -m "chore: scaffold whereami package"
```

---

## Task 2: Cache module

**Files:**
- Create: `src/whereami/cache.py`
- Test: `tests/test_cache.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cache.py
import json
from whereami import cache


def test_load_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    assert cache.load_cache("sess-1") == {}


def test_save_then_load_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache.save_cache("sess-1", {"score": 50, "turns_seen": 3})
    assert cache.load_cache("sess-1") == {"score": 50, "turns_seen": 3}


def test_save_creates_dir_and_is_session_scoped(tmp_path, monkeypatch):
    nested = tmp_path / "whereami"
    monkeypatch.setattr(cache, "CACHE_DIR", nested)
    cache.save_cache("a", {"score": 1})
    cache.save_cache("b", {"score": 2})
    assert cache.load_cache("a") == {"score": 1}
    assert cache.load_cache("b") == {"score": 2}


def test_load_corrupt_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    (tmp_path / "sess-1.json").write_text("{not json")
    assert cache.load_cache("sess-1") == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cache.py -v`
Expected: FAIL — `ModuleNotFoundError` / `AttributeError: module 'whereami.cache' has no attribute ...`

- [ ] **Step 3: Write minimal implementation**

```python
# src/whereami/cache.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cache.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/whereami/cache.py tests/test_cache.py
git commit -m "feat: per-session cache with atomic writes"
```

---

## Task 3: Transcript parsing — genuine human turns (spec risk A)

**Files:**
- Create: `src/whereami/transcript.py`
- Test: `tests/test_transcript.py`

This is the highest-risk module. Claude Code logs tool results and injected context as `user`-role entries; we must extract only what the human actually typed.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_transcript.py
import json
from whereami import transcript


def _line(obj):
    return json.dumps(obj)


def test_plain_string_user_turn_is_human():
    entry = {"type": "user", "message": {"role": "user", "content": "fix the bug"}}
    assert transcript.human_text(entry) == "fix the bug"


def test_tool_result_entry_is_not_human():
    entry = {"type": "user", "message": {"role": "user",
             "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}}
    assert transcript.human_text(entry) is None


def test_system_reminder_is_stripped():
    entry = {"type": "user", "message": {"role": "user",
             "content": "<system-reminder>noise\nmore</system-reminder>real question"}}
    assert transcript.human_text(entry) == "real question"


def test_reminder_only_turn_is_not_human():
    entry = {"type": "user", "message": {"role": "user",
             "content": "<system-reminder>just noise</system-reminder>"}}
    assert transcript.human_text(entry) is None


def test_meta_and_sidechain_entries_skipped():
    assert transcript.human_text({"type": "user", "isMeta": True,
                                  "message": {"role": "user", "content": "hi"}}) is None
    assert transcript.human_text({"type": "user", "isSidechain": True,
                                  "message": {"role": "user", "content": "hi"}}) is None


def test_assistant_entry_is_not_human():
    assert transcript.human_text({"type": "assistant",
                                  "message": {"role": "assistant", "content": "answer"}}) is None


def test_text_blocks_joined_and_tool_results_dropped():
    entry = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": "junk"},
        {"type": "text", "text": "do the thing"},
    ]}}
    assert transcript.human_text(entry) == "do the thing"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_transcript.py -v`
Expected: FAIL — `AttributeError: module 'whereami.transcript' has no attribute 'human_text'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/whereami/transcript.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_transcript.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/whereami/transcript.py tests/test_transcript.py
git commit -m "feat: extract genuine human turns from transcript entries"
```

---

## Task 4: Transcript file readers — tail-from-end + head (spec risk C)

**Files:**
- Modify: `src/whereami/transcript.py`
- Test: `tests/test_transcript.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_transcript.py  (append)
def test_last_human_text_reads_from_end(tmp_path):
    p = tmp_path / "t.jsonl"
    lines = [
        _line({"type": "user", "message": {"role": "user", "content": "first ask"}}),
        _line({"type": "assistant", "message": {"role": "assistant", "content": "reply"}}),
        _line({"type": "user", "message": {"role": "user",
               "content": [{"type": "tool_result", "content": "tool output"}]}}),
        _line({"type": "user", "message": {"role": "user", "content": "second ask"}}),
        _line({"type": "assistant", "message": {"role": "assistant", "content": "reply2"}}),
    ]
    p.write_text("\n".join(lines) + "\n")
    assert transcript.last_human_text(str(p)) == "second ask"


def test_last_human_text_missing_file_returns_none():
    assert transcript.last_human_text("/no/such/file.jsonl") is None


def test_opening_turns_reads_from_start(tmp_path):
    p = tmp_path / "t.jsonl"
    lines = [
        _line({"type": "user", "isMeta": True,
               "message": {"role": "user", "content": "session start hook"}}),
        _line({"type": "user", "message": {"role": "user", "content": "goal one"}}),
        _line({"type": "assistant", "message": {"role": "assistant", "content": "ok"}}),
        _line({"type": "user", "message": {"role": "user", "content": "goal two"}}),
    ]
    p.write_text("\n".join(lines) + "\n")
    assert transcript.opening_turns(str(p), n=2) == ["goal one", "goal two"]


def test_recent_turns_returns_last_n_human(tmp_path):
    p = tmp_path / "t.jsonl"
    lines = [_line({"type": "user", "message": {"role": "user", "content": f"ask {i}"}})
             for i in range(10)]
    p.write_text("\n".join(lines) + "\n")
    assert transcript.recent_turns(str(p), n=3) == ["ask 7", "ask 8", "ask 9"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_transcript.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'last_human_text'`

- [ ] **Step 3: Write minimal implementation (append to `transcript.py`)**

```python
# src/whereami/transcript.py  (append)
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


def last_human_text(path: str) -> Optional[str]:
    for line in reversed(_tail_lines(path)):
        entry = _parse(line)
        if entry is None:
            continue
        text = human_text(entry)
        if text:
            return text
    return None


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_transcript.py -v`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add src/whereami/transcript.py tests/test_transcript.py
git commit -m "feat: tail-from-end and head transcript readers"
```

---

## Task 5: Drift scoring via Haiku (mockable)

**Files:**
- Create: `src/whereami/drift.py`
- Test: `tests/test_drift.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_drift.py
from whereami import drift


class _FakeMessages:
    def __init__(self, text):
        self._text = text

    def create(self, **kwargs):
        class _Block:
            type = "text"
            text = self._text
        class _Resp:
            content = [_Block()]
        return _Resp()


class _FakeClient:
    def __init__(self, text):
        self.messages = _FakeMessages(text)


def test_score_drift_parses_json():
    client = _FakeClient('{"score": 72, "label": "drifted to deploy config"}')
    score, label = drift.score_drift("build a parser", "fix the CI deploy", client=client)
    assert score == 72
    assert label == "drifted to deploy config"


def test_score_drift_clamps_and_handles_junk():
    client = _FakeClient("sorry I cannot help")
    score, label = drift.score_drift("x", "y", client=client)
    assert score == 0
    assert label == ""


def test_score_drift_clamps_out_of_range():
    client = _FakeClient('{"score": 250, "label": "way off"}')
    score, label = drift.score_drift("x", "y", client=client)
    assert score == 100
    assert label == "way off"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_drift.py -v`
Expected: FAIL — `AttributeError: module 'whereami.drift' has no attribute 'score_drift'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/whereami/drift.py
import json
import re
from typing import Optional, Tuple

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


def _extract_text(resp) -> str:
    parts = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts)


def score_drift(goal: str, recent: str, client=None) -> Tuple[int, str]:
    if client is None:
        import anthropic
        client = anthropic.Anthropic()
    resp = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=200,
        messages=[{"role": "user",
                   "content": _PROMPT.format(goal=goal, recent=recent)}],
    )
    text = _extract_text(resp)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_drift.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/whereami/drift.py tests/test_drift.py
git commit -m "feat: Haiku drift scoring with defensive JSON parsing"
```

---

## Task 6: Drift compute orchestration

**Files:**
- Modify: `src/whereami/drift.py`
- Test: `tests/test_drift.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_drift.py  (append)
from whereami import cache


def test_compute_writes_score_and_counters(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    # transcript with an opening goal and recent activity
    import json
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join([
        json.dumps({"type": "user", "message": {"role": "user", "content": "build a parser"}}),
        json.dumps({"type": "user", "message": {"role": "user", "content": "now fix deploy"}}),
    ]) + "\n")
    cache.save_cache("s1", {"turns_seen": 6, "turns_at_last_compute": 0})

    monkeypatch.setattr(drift, "score_drift", lambda g, r, client=None: (55, "on deploy"))
    drift.compute("s1", str(p))

    out = cache.load_cache("s1")
    assert out["score"] == 55
    assert out["label"] == "on deploy"
    assert out["turns_at_last_compute"] == 6
    assert out["opening_goal"]
    assert out["ts"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_drift.py::test_compute_writes_score_and_counters -v`
Expected: FAIL — `AttributeError: module 'whereami.drift' has no attribute 'compute'`

- [ ] **Step 3: Write minimal implementation (append to `drift.py`)**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_drift.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/whereami/drift.py tests/test_drift.py
git commit -m "feat: drift compute orchestration writes cache"
```

---

## Task 7: Stop-hook entry — turn counter + detached spawn (spec risk B)

**Files:**
- Modify: `src/whereami/drift.py`
- Test: `tests/test_drift.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_drift.py  (append)
import io
import sys


def _run_hook_with(monkeypatch, payload, spawned):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(drift, "_spawn_compute", lambda sid, path: spawned.append((sid, path)))
    try:
        drift.run_hook()
    except SystemExit:
        pass


def test_hook_increments_counter_each_call(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    spawned = []
    payload = {"session_id": "s1", "transcript_path": str(tmp_path / "t.jsonl")}
    _run_hook_with(monkeypatch, payload, spawned)
    _run_hook_with(monkeypatch, payload, spawned)
    assert cache.load_cache("s1")["turns_seen"] == 2


def test_hook_spawns_on_first_turn_then_every_sixth(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    spawned = []
    payload = {"session_id": "s1", "transcript_path": str(tmp_path / "t.jsonl")}
    # simulate having already computed once at turn 1
    cache.save_cache("s1", {"turns_seen": 1, "turns_at_last_compute": 1, "ts": "x"})
    for _ in range(6):
        _run_hook_with(monkeypatch, payload, spawned)
    # turns go 2..7; spawn happens when 7 - 1 >= 6, i.e. exactly once
    assert len(spawned) == 1


def test_hook_spawns_first_time_when_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    spawned = []
    payload = {"session_id": "s1", "transcript_path": str(tmp_path / "t.jsonl")}
    _run_hook_with(monkeypatch, payload, spawned)
    assert len(spawned) == 1  # no prior ts → compute immediately
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_drift.py -k hook -v`
Expected: FAIL — `AttributeError: module 'whereami.drift' has no attribute 'run_hook'`

- [ ] **Step 3: Write minimal implementation (append to `drift.py`)**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_drift.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/whereami/drift.py tests/test_drift.py
git commit -m "feat: Stop-hook entry with message-count throttle and detached compute"
```

---

## Task 8: Statusline renderer (spec risk D — cost optional)

**Files:**
- Create: `src/whereami/statusline.py`
- Test: `tests/test_statusline.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_statusline.py
from whereami import statusline, cache


def test_light_buckets():
    assert statusline.light_emoji(None) == "⚪"   # no data
    assert statusline.light_emoji(10) == "\U0001f7e2"  # green
    assert statusline.light_emoji(50) == "\U0001f7e1"  # amber
    assert statusline.light_emoji(90) == "\U0001f534"  # red


def test_fmt_duration():
    assert statusline.fmt_duration(45_000) == "45s"
    assert statusline.fmt_duration(125_000) == "2m"
    assert statusline.fmt_duration(3_720_000) == "1h2m"


def test_truncate():
    assert statusline.truncate("short", 60) == "short"
    assert statusline.truncate("x" * 80, 60) == "x" * 59 + "…"


def test_render_includes_light_and_last_said(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache.save_cache("s1", {"score": 80})
    p = tmp_path / "t.jsonl"
    import json
    p.write_text(json.dumps(
        {"type": "user", "message": {"role": "user", "content": "what did I ask"}}) + "\n")
    line = statusline.render({
        "session_id": "s1",
        "transcript_path": str(p),
        "cost": {"total_duration_ms": 125_000, "total_cost_usd": 0.0},
    })
    assert "\U0001f534" in line          # red light from score 80
    assert "what did I ask" in line
    assert "2m" in line


def test_render_omits_cost_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.delenv("WHEREAMI_SHOW_COST", raising=False)
    line = statusline.render({
        "session_id": "s1", "transcript_path": str(tmp_path / "none.jsonl"),
        "cost": {"total_cost_usd": 1.23},
    })
    assert "1.23" not in line


def test_render_shows_cost_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("WHEREAMI_SHOW_COST", "1")
    line = statusline.render({
        "session_id": "s1", "transcript_path": str(tmp_path / "none.jsonl"),
        "cost": {"total_cost_usd": 1.23},
    })
    assert "1.23" in line
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_statusline.py -v`
Expected: FAIL — `AttributeError: module 'whereami.statusline' has no attribute 'light_emoji'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/whereami/statusline.py
import json
import os
import sys
from typing import Optional

from . import cache, transcript


def light_emoji(score: Optional[int]) -> str:
    if score is None:
        return "⚪"  # white circle: no data yet
    if score <= 33:
        return "\U0001f7e2"  # green
    if score <= 66:
        return "\U0001f7e1"  # amber
    return "\U0001f534"      # red


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def fmt_duration(ms: int) -> str:
    secs = int(ms // 1000)
    if secs < 60:
        return "{}s".format(secs)
    mins = secs // 60
    if mins < 60:
        return "{}m".format(mins)
    return "{}h{}m".format(mins // 60, mins % 60)


def context_pct(data: dict) -> Optional[int]:
    """Return context-window fill % if the harness payload exposes it; else None.
    Version-dependent — we never reverse-engineer it from the transcript."""
    ctx = data.get("context")
    if isinstance(ctx, dict):
        for key in ("used_pct", "percent", "used_percent"):
            val = ctx.get(key)
            if isinstance(val, (int, float)):
                return int(val)
    return None


def render(data: dict) -> str:
    session_id = data.get("session_id", "")
    transcript_path = data.get("transcript_path", "")
    cached = cache.load_cache(session_id)

    segs = [light_emoji(cached.get("score"))]

    last = transcript.last_human_text(transcript_path) if transcript_path else None
    if last:
        segs.append('"{}"'.format(truncate(last.replace("\n", " "), 60)))

    cost = data.get("cost") or {}
    dur = cost.get("total_duration_ms")
    if dur:
        segs.append("⏱ " + fmt_duration(dur))

    pct = context_pct(data)
    if pct is not None:
        segs.append("\U0001f522 {}%".format(pct))

    if os.environ.get("WHEREAMI_SHOW_COST"):
        usd = cost.get("total_cost_usd")
        if usd:
            segs.append("\U0001f4b2{:.2f}".format(usd))

    return " · ".join(segs)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        print("")
        return
    print(render(data))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_statusline.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all tests pass (cache + transcript + drift + statusline)

- [ ] **Step 6: Commit**

```bash
git add src/whereami/statusline.py tests/test_statusline.py
git commit -m "feat: statusline renderer with coherence light, last-said, optional cost"
```

---

## Task 9: The `/whereami` deep-view skill

**Files:**
- Create: `.claude/skills/whereami/SKILL.md`

This skill is read-only instructions for Claude. It must not write files (so `Esc Esc` rewind fully cleans it up).

- [ ] **Step 1: Create the skill file**

```markdown
---
name: whereami
description: Read-only in-session reorientation. Use when you (the user) have lost track of a long or resumed session and want a quick rich summary of what's going on — what you last said, the original goal vs. now, the open loop, drift, and whether to split the session. Tip: after reading, press Esc Esc to rewind this skill out of the conversation so it leaves no artifact.
---

# /whereami — Where am I in this session?

You are producing a **read-only** reorientation snapshot. Do NOT edit, write, or
create any files. Do NOT run commands with side effects. The user will likely
`Esc Esc` to rewind this output away afterward, so it must leave nothing behind.

Using the conversation already in your context (and, only if useful for exact
timestamps, reading the current session's transcript file), output these five
sections, concise and skimmable:

## 1. Last thing you said
Quote the user's most recent genuine message verbatim (not tool results).

## 2. Original goal vs. now
One line on what this session set out to do (from the first real user
message), and one line on what it's actually doing now.

## 3. Open loop / your turn
What is the agent currently waiting on or what did the last response ask of
the user? What is the next concrete action?

## 4. Drift
State whether the conversation has stayed on its original path or wandered, and
how far. If a drift score cache exists at `~/.claude/whereami/<session-id>.json`,
you may read it for the score/label, but do not write to it.

## 5. Split recommendation
A clear call: keep going in this session, or start a fresh one — with a
one-sentence reason (e.g. context is full, topic has fully changed, original
task is done).

Keep the whole thing tight — this is a glance, not a report.
```

- [ ] **Step 2: Verify the skill is discoverable**

Run: `ls .claude/skills/whereami/SKILL.md`
Expected: the file path prints (exists).

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/whereami/SKILL.md
git commit -m "feat: read-only /whereami deep-view skill"
```

---

## Task 10: Wiring (settings.json) + end-to-end verification

**Files:**
- Create: `.claude/settings.json`

The statusLine and Stop hook reference the venv's console scripts by absolute
path so they work regardless of the active shell.

- [ ] **Step 1: Resolve the absolute venv script paths**

Run: `echo "$(pwd)/.venv/bin/whereami-statusline"; echo "$(pwd)/.venv/bin/whereami-hook"`
Expected: two absolute paths print. Use them verbatim in the next step.

- [ ] **Step 2: Create `.claude/settings.json`**

Replace `<ABS>` with the absolute project path from Step 1.

```json
{
  "statusLine": {
    "type": "command",
    "command": "<ABS>/.venv/bin/whereami-statusline"
  },
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "<ABS>/.venv/bin/whereami-hook"
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 3: Verify the statusline renders from a simulated payload**

Run:
```bash
printf '%s' '{"session_id":"verify","transcript_path":"/tmp/none.jsonl","cost":{"total_duration_ms":125000}}' | .venv/bin/whereami-statusline
```
Expected: a line beginning with the white circle ⚪ (no cache yet) and containing `⏱ 2m`.

- [ ] **Step 4: Verify the hook increments and spawns from a simulated payload**

Run:
```bash
printf '%s' '{"session_id":"verify","transcript_path":"/tmp/none.jsonl"}' | .venv/bin/whereami-hook
cat ~/.claude/whereami/verify.json
```
Expected: hook exits 0; cache file shows `"turns_seen": 1`. (The detached compute will no-op because `/tmp/none.jsonl` has no turns — that is expected.)

- [ ] **Step 5: Real end-to-end check** (requires `ANTHROPIC_API_KEY`)

This is the first step that makes a live Haiku call. Confirm a key is set
(`echo "${ANTHROPIC_API_KEY:+present}"` should print `present`). If it is not
available at build time, stop here and hand Steps 4–5 to the user.

Open a NEW Claude Code session in this project directory, exchange a few
messages on a clear topic, then deliberately change topics. After the 6th
assistant turn, confirm:
- the statusline shows your last message + a coherence light
- `cat ~/.claude/whereami/<that-session-id>.json` shows a `score` and `label`
- running `/whereami` prints the five-section summary, and `Esc Esc` removes it

- [ ] **Step 6: Commit**

```bash
git add .claude/settings.json
git commit -m "feat: wire statusline and Stop hook in settings.json"
```

---

## Self-Review

**Spec coverage:**
- Statusline renderer (light, last-said, elapsed, ctx%, optional cost) → Task 8 ✅
- Drift sidecar (Stop hook, message-count throttle = 6, detached Haiku, cache) → Tasks 5–7 ✅
- `/whereami` read-only skill (five sections, Esc-Esc-friendly) → Task 9 ✅
- Wiring in settings.json → Task 10 ✅
- Risk A (genuine human turns / strip injected noise) → Task 3 ✅
- Risk B (non-blocking Stop hook / detached spawn) → Task 7 ✅
- Risk C (tail-from-end, no full-file parse per tick) → Task 4 + Task 8 ✅
- Risk D (cost optional / env-gated) → Task 8 ✅
- Cache schema + throttle counters → Task 2, refined in Tasks 6–7 ✅

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every test step shows full assertions. The only intentional template token is `<ABS>` in Task 10, with explicit instructions to resolve it in Step 1.

**Type consistency:** `human_text` (Task 3) used by `last_human_text`/`opening_turns`/`recent_turns` (Task 4), all consumed by `compute` (Task 6) and `render`/`last_human_text` (Task 8). `score_drift(goal, recent, client=None)` signature consistent across Tasks 5–6. `cache.load_cache`/`save_cache` signatures consistent across Tasks 2, 6, 7, 8. Cache keys (`score`, `label`, `ts`, `opening_goal`, `turns_seen`, `turns_at_last_compute`) consistent across Tasks 6, 7, 8. `THROTTLE_TURNS = 6` matches the approved spec. ✅
