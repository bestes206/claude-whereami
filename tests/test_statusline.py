# tests/test_statusline.py
from whereami import statusline, cache


def test_light_buckets():
    # color carried by ANSI escape; glyph is a text-width dot, not an emoji
    assert statusline.light(None) == "\033[90m●\033[0m"   # dim grey: no data
    assert statusline.light(10) == "\033[32m●\033[0m"     # green
    assert statusline.light(50) == "\033[33m●\033[0m"     # amber
    assert statusline.light(90) == "\033[31m●\033[0m"     # red


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
    assert "\033[31m●\033[0m" in line    # red light from score 80
    assert "what did I ask" in line
    assert "2m" in line


def test_context_pct_from_payload():
    assert statusline.context_pct({"context_window": {"used_percentage": 8}}) == 8
    assert statusline.context_pct({"context_window": {"used_percentage": 8.6}}) == 9
    assert statusline.context_pct({}) is None
    assert statusline.context_pct({"context_window": {}}) is None


def test_context_pct_derived_from_tokens():
    data = {"context_window": {
        "context_window_size": 200000,
        "current_usage": {"input_tokens": 10000, "cache_read_input_tokens": 10000},
    }}
    assert statusline.context_pct(data) == 10


def test_render_includes_context_pct(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    line = statusline.render({
        "session_id": "s1", "transcript_path": str(tmp_path / "none.jsonl"),
        "context_window": {"used_percentage": 42},
    })
    assert "42%" in line


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
