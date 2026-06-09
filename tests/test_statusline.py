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
