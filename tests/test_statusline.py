# tests/test_statusline.py
import json

from whereami import cache, statusline

GREEN, AMBER, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[90m", "\033[0m"


def test_fmt_duration():
    assert statusline.fmt_duration(45_000) == "45s"
    assert statusline.fmt_duration(125_000) == "2m"
    assert statusline.fmt_duration(3_720_000) == "1h2m"


def test_fmt_duration_negative_clamped_to_zero():
    # A clock-skewed payload must not render "⏱ -5s"; fmt_ago already clamps.
    assert statusline.fmt_duration(-5_000) == "0s"


def test_truncate():
    assert statusline.truncate("short", 60) == "short"
    assert statusline.truncate("x" * 80, 60) == "x" * 59 + "…"


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
    assert "⊠ 42%" in line


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


def test_gist_segment_colors_words_by_score():
    assert statusline.gist_segment({"score": 10, "gist": "parser retry logic"}) == \
        GREEN + "parser retry logic" + RESET
    assert statusline.gist_segment({"score": 50, "gist": "parser retry logic"}) == \
        AMBER + "parser retry logic" + RESET
    assert statusline.gist_segment({"score": 90, "gist": "parser retry logic"}) == \
        RED + "parser retry logic" + RESET


def test_gist_segment_placeholder_when_missing_score_or_gist():
    assert statusline.gist_segment({}) == DIM + "…" + RESET
    # v1 cache: score present, gist absent → placeholder, never an empty colored void
    assert statusline.gist_segment({"score": 42, "label": "v1 label"}) == DIM + "…" + RESET
    assert statusline.gist_segment({"gist": "no score"}) == DIM + "…" + RESET


def test_render_normal_two_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache.save_cache("s1", {"score": 80, "gist": "CI retry backoff logic"})
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps(
        {"type": "user", "message": {"role": "user",
                                     "content": "make it\nexponential"}}) + "\n")
    out = statusline.render({
        "session_id": "s1", "transcript_path": str(p),
        "cost": {"total_duration_ms": 125_000},
        "context_window": {"used_percentage": 63},
    })
    lines = out.split("\n")
    assert len(lines) == 2
    assert RED + "CI retry backoff logic" + RESET in lines[0]
    assert "⏱ 2m" in lines[0] and "⊠ 63%" in lines[0]
    assert lines[1] == "❯ make it exponential"   # newlines collapsed, head kept


def test_render_normal_line2_truncates_at_150(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps(
        {"type": "user", "message": {"role": "user", "content": "x" * 200}}) + "\n")
    out = statusline.render({"session_id": "s1", "transcript_path": str(p)})
    line2 = out.split("\n")[1]
    assert line2 == "❯ " + "x" * 149 + "…"


def test_render_degrades_to_one_line_without_message(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    out = statusline.render({"session_id": "s1",
                             "transcript_path": str(tmp_path / "none.jsonl")})
    assert "\n" not in out
    assert DIM + "…" + RESET in out


def test_gist_segment_non_finite_score_is_placeholder():
    assert statusline.gist_segment({"score": float("nan"), "gist": "x"}) == DIM + "…" + RESET
    assert statusline.gist_segment({"score": float("inf"), "gist": "x"}) == DIM + "…" + RESET


def test_ansi_and_control_chars_stripped_from_model_fields():
    # JSON \u escapes can smuggle ESC/BEL into model-authored fields (a
    # prompt-injected gist setting the window title, say); whole CSI/OSC
    # sequences vanish, bare control bytes vanish, words survive.
    assert statusline.gist_segment(
        {"score": 5, "gist": "fix\x1b]0;pwned\x07 bug"}) == GREEN + "fix bug" + RESET
    assert statusline.gist_segment(
        {"score": 5, "gist": "a\x1b[31mb\x1b[0mc"}) == GREEN + "abc" + RESET


def test_ansi_stripped_from_pasted_last_message(tmp_path, monkeypatch):
    # A pasted shell log re-emitted raw ANSI into the terminal every tick.
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "user", "message": {
        "role": "user",
        "content": "\x1b[31mFAIL\x1b[0m tests \x07 then go"}}) + "\n")
    out = statusline.render({"session_id": "s1", "transcript_path": str(p)})
    assert out.split("\n")[1] == "❯ FAIL tests then go"


def test_gist_segment_bool_score_is_placeholder():
    # bool subclasses int: a true/false score is cache garbage, not a green 1.
    assert statusline.gist_segment({"score": True, "gist": "x"}) == DIM + "…" + RESET
    assert statusline.gist_segment({"score": False, "gist": "x"}) == DIM + "…" + RESET


def test_main_never_breaks_on_hostile_payload(monkeypatch, capsys):
    import io
    import sys as _sys
    for payload in ('{"cost": [1, 2]}',
                    '{"session_id": null, "transcript_path": "/t"}',
                    '{"cost": {"total_duration_ms": "abc"}}',
                    'not json at all'):
        monkeypatch.setattr(_sys, "stdin", io.StringIO(payload))
        statusline.main()   # must not raise
        assert capsys.readouterr().out.endswith("\n")


def test_model_authored_newlines_are_collapsed(tmp_path, monkeypatch):
    # Newlines in the gist must not add rows or orphan the per-line ANSI
    # reset (color bleed).
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache.save_cache("s1", {"score": 20, "gist": "parser\nwork"})
    out = statusline.render({"session_id": "s1", "transcript_path": ""})
    assert out.split("\n")[0].startswith(GREEN + "parser work" + RESET)
    assert statusline.gist_segment({"score": 5, "gist": "a\nb"}) == GREEN + "a b" + RESET


def _patch_spawn(monkeypatch, calls):
    from whereami import drift
    monkeypatch.setattr(drift, "maybe_spawn_compute",
                        lambda sid, path, **kwargs: calls.append(sid))


def test_renderer_never_spawns_a_compute(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    calls = []
    _patch_spawn(monkeypatch, calls)
    cache.save_turns("s1", 8)   # stale: the renderer still never spawns
    statusline.render({"session_id": "s1", "transcript_path": "/t"})
    assert calls == []


