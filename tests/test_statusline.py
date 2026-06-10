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


import os
from datetime import datetime


def _iso(epoch):
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _touch_peek(tmp_path, mtime):
    p = tmp_path / "peek"
    p.touch()
    os.utime(str(p), (mtime, mtime))
    return p


def test_peek_active_window_and_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    assert statusline.peek_active(1000.0) is False   # no peek file
    _touch_peek(tmp_path, 1000.0)
    assert statusline.peek_active(1000.0 + statusline.PEEK_SECONDS - 1) is True
    assert statusline.peek_active(1000.0 + statusline.PEEK_SECONDS) is False


def test_peek_future_mtime_is_inert(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    _touch_peek(tmp_path, 2000.0)   # clock stepped: mtime in the future
    assert statusline.peek_active(1000.0) is False


def test_peek_repress_extends_window(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    _touch_peek(tmp_path, 1000.0)
    expired = 1000.0 + statusline.PEEK_SECONDS + 5
    assert statusline.peek_active(expired) is False
    _touch_peek(tmp_path, expired)   # re-press refreshes the mtime
    assert statusline.peek_active(expired + 1) is True


def test_fmt_ago():
    assert statusline.fmt_ago(45) == "45s"
    assert statusline.fmt_ago(240) == "4m"
    assert statusline.fmt_ago(7200) == "2h"
    assert statusline.fmt_ago(3 * 86_400) == "3d"
    assert statusline.fmt_ago(-5) == "0s"


def test_peek_staleness_strings():
    now = 100_000.0
    assert statusline.staleness_segment({"ts": _iso(now - 240)}, now) == "scored 4m ago"
    assert statusline.staleness_segment({"ts": _iso(now - 3 * 86_400)}, now) == "scored 3d ago"
    assert statusline.staleness_segment({}, now) == "no score yet"


def test_peek_failure_segment_only_when_failure_newer_than_ts():
    now = 100_000.0
    cached = {"ts": _iso(now - 600), "last_failure_ts": _iso(now - 120)}
    assert statusline.failure_segment(cached, now) == "last compute failed 2m ago"
    cached = {"ts": _iso(now - 60), "last_failure_ts": _iso(now - 120)}
    assert statusline.failure_segment(cached, now) is None
    assert statusline.failure_segment({}, now) is None


def test_split_hint_both_arms():
    assert statusline.split_hint({"score": 86}, None) is True   # score alone
    assert statusline.split_hint({"score": 70}, 71) is True     # score + context
    assert statusline.split_hint({"score": 70}, 60) is False
    assert statusline.split_hint({"score": 50}, 90) is False
    assert statusline.split_hint({}, 90) is False


def test_peek_full_panel_golden(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("COLUMNS", "100")
    now = 10_000.0
    _touch_peek(tmp_path, now - 1)
    cache.save_cache("s1", {"score": 58, "gist": "CI retry backoff logic",
                            "open_loop": "choose a backoff strategy",
                            "goal": "in-session reorientation tool",
                            "ts": _iso(now - 240), "turns_at_last_compute": 12})
    cache.save_turns("s1", 12)   # no turns since the score → ⊙ bright
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "user", "message": {
        "role": "user", "content": "ok now make the retry logic exponential"}}) + "\n")
    out = statusline.render({
        "session_id": "s1", "transcript_path": str(p),
        "cost": {"total_duration_ms": 2_520_000},
        "context_window": {"used_percentage": 63},
    }, now=now)
    lines = out.split("\n")
    assert lines[0] == (AMBER + "drift 58 · CI retry backoff logic" + RESET
                        + "  " + DIM + "(goal: in-session reorientation tool)" + RESET)
    assert lines[1] == "❯ ok now make the retry logic exponential"
    assert lines[2] == "⊙ your turn: choose a backoff strategy"
    assert lines[3] == "⏱ 42m · ⊠ 63% · scored 4m ago"


def test_peek_open_loop_dim_when_stale_omitted_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    cache.save_cache("s1", {"score": 10, "gist": "parser work",
                            "open_loop": "pick a name", "ts": _iso(now - 60),
                            "turns_at_last_compute": 6})
    cache.save_turns("s1", 8)   # turns have passed → presumptively answered → dim
    out = statusline.render({"session_id": "s1", "transcript_path": ""}, now=now)
    assert DIM + "⊙ your turn: pick a name" + RESET in out
    data = cache.load_cache("s1")
    data["open_loop"] = ""
    cache.save_cache("s1", data)
    out = statusline.render({"session_id": "s1", "transcript_path": ""}, now=now)
    assert "⊙" not in out   # empty open loop → line omitted, never fabricated


def test_split_hint_rendered_in_peek_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    cache.save_cache("s1", {"score": 90, "gist": "totally different work",
                            "ts": _iso(now - 60), "turns_at_last_compute": 0})
    out = statusline.render({"session_id": "s1", "transcript_path": ""}, now=now)
    assert out.split("\n")[-1].endswith("· split?")


def test_no_split_hint_on_unscored_panel(tmp_path, monkeypatch):
    # A v1 cache (score, no gist) renders the dim not-yet-scored placeholder;
    # the same frame must not also recommend a split from that unusable score.
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    cache.save_cache("s1", {"score": 90, "label": "v1", "ts": _iso(now - 60)})
    out = statusline.render({"session_id": "s1", "transcript_path": "",
                             "context_window": {"used_percentage": 90}}, now=now)
    assert out.split("\n")[0] == DIM + "…" + RESET
    assert "split?" not in out


def test_peek_message_wraps_to_four_lines_with_ellipsis(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("COLUMNS", "100")
    now = 10_000.0
    _touch_peek(tmp_path, now)
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "user", "message": {
        "role": "user", "content": "word " * 200}}) + "\n")   # ~1000 chars
    out = statusline.render({"session_id": "s1", "transcript_path": str(p)}, now=now)
    msg_lines = [l for l in out.split("\n") if l.startswith("❯") or l.startswith("  ")]
    assert len(msg_lines) == 4
    assert msg_lines[-1].endswith("…")
    assert all(len(l) <= 100 for l in msg_lines)


def test_wrap_message_exact_ceiling_boundary():
    # A width wider than the ceiling isolates the char limit from the line cap.
    wide = statusline.PEEK_MSG_LIMIT + 10
    exact = "x" * statusline.PEEK_MSG_LIMIT
    assert statusline.wrap_message(exact, wide) == ["❯ " + exact]
    over = "x" * (statusline.PEEK_MSG_LIMIT + 1)
    assert statusline.wrap_message(over, wide) == ["❯ " + exact + "…"]


def test_peek_no_cache_degraded_panel(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "user", "message": {
        "role": "user", "content": "first ask"}}) + "\n")
    out = statusline.render({"session_id": "s1", "transcript_path": str(p),
                             "context_window": {"used_percentage": 5}}, now=now)
    lines = out.split("\n")
    assert lines[0] == DIM + "…" + RESET   # no "drift —", no empty goal parenthetical
    assert lines[1] == "❯ first ask"
    assert "⊙" not in out
    assert lines[-1] == "⊠ 5% · no score yet"


def test_peek_v1_cache_renders_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    cache.save_cache("s1", {"score": 42, "label": "v1 label", "ts": _iso(now - 60),
                            "turns_seen": 9, "turns_at_last_compute": 9})
    out = statusline.render({"session_id": "s1", "transcript_path": ""}, now=now)
    assert out.split("\n")[0] == DIM + "…" + RESET


def test_peek_bool_score_degrades_to_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    cache.save_cache("s1", {"score": True, "gist": "parser work",
                            "ts": _iso(now - 60)})
    out = statusline.render({"session_id": "s1", "transcript_path": ""}, now=now)
    assert out.split("\n")[0] == DIM + "…" + RESET


def test_peek_goal_falls_back_to_opening_goal_head(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    cache.save_cache("s1", {"score": 10, "gist": "parser work",
                            "opening_goal": "g" * 80, "ts": _iso(now - 60)})
    out = statusline.render({"session_id": "s1", "transcript_path": ""}, now=now)
    assert "(goal: " + "g" * 39 + "…)" in out


def test_peek_malformed_minor_fields_degrade_alone(tmp_path, monkeypatch):
    # A bad minor field must not blank a panel that has a good score+gist.
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    cache.save_cache("s1", {"score": 20, "gist": "parser work",
                            "open_loop": ["not", "a", "string"],
                            "turns_at_last_compute": "nine",
                            "opening_goal": 12345,
                            "ts": _iso(now - 60)})
    out = statusline.render({"session_id": "s1", "transcript_path": ""}, now=now)
    lines = out.split("\n")
    assert "parser work" in lines[0]      # good fields survive
    assert "⊙" not in out                 # bad open_loop omitted, not rendered/crashed
    assert "(goal:" not in out            # non-str opening_goal → parenthetical omitted


def test_model_authored_newlines_are_collapsed(tmp_path, monkeypatch):
    # Newlines in gist/goal/open_loop must not add panel rows or orphan the
    # per-line ANSI reset (color bleed).
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    cache.save_cache("s1", {"score": 20, "gist": "parser\nwork",
                            "open_loop": "pick\na name",
                            "goal": "goal\nline", "ts": _iso(now - 60),
                            "turns_at_last_compute": 0})
    cache.save_turns("s1", 0)
    out = statusline.render({"session_id": "s1", "transcript_path": ""}, now=now)
    for line in out.split("\n"):
        assert "\r" not in line
        # Each line that has any ANSI escape must have an even count
        # (every open has a paired close on the same line).
        # A line with DIM+text+RESET has exactly 2 escapes → even.
        # Color bleed (open without reset) → odd.
        if "\033[" in line:
            assert line.count("\033[") % 2 == 0, \
                "ANSI bleed detected on line: {!r}".format(line)
    assert "parser work" in out.split("\n")[0]   # collapsed, single row
    assert "⊙ your turn: pick a name" in out
    assert "(goal: goal line)" in out
    # normal mode too: gist_segment is the always-on path
    assert statusline.gist_segment({"score": 5, "gist": "a\nb"}) == GREEN + "a b" + RESET


def test_peek_cached_goal_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    cache.save_cache("s1", {"score": 10, "gist": "parser work",
                            "goal": "G" * 80, "ts": _iso(now - 60)})
    out = statusline.render({"session_id": "s1", "transcript_path": ""}, now=now)
    assert "(goal: " + "G" * 39 + "…)" in out


# ---------------------------------------------------------------------------
# Task 8: renderer-side recompute trigger
# ---------------------------------------------------------------------------

def _patch_spawn(monkeypatch, calls):
    from whereami import drift
    monkeypatch.setattr(drift, "maybe_spawn_compute",
                        lambda sid, path, now=None, spawner=None: calls.append(sid))


def test_peek_triggers_recompute_when_turns_advanced(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    calls = []
    _patch_spawn(monkeypatch, calls)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    cache.save_cache("s1", {"score": 10, "gist": "parser work",
                            "ts": _iso(now - 60), "turns_at_last_compute": 6})
    cache.save_turns("s1", 8)   # turn-delta arm
    statusline.render({"session_id": "s1", "transcript_path": "/t"}, now=now)
    assert calls == ["s1"]


def test_peek_triggers_recompute_when_gist_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    calls = []
    _patch_spawn(monkeypatch, calls)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    cache.save_cache("s1", {"score": 42, "label": "v1", "ts": _iso(now - 60),
                            "turns_at_last_compute": 6})
    cache.save_turns("s1", 6)   # no turn delta — the gist arm alone must fire
    statusline.render({"session_id": "s1", "transcript_path": "/t"}, now=now)
    assert calls == ["s1"]


def test_peek_recompute_gist_arm_survives_corrupt_talc(tmp_path, monkeypatch):
    # hook_due checks the gist arm before the turn-delta; the renderer must
    # too — a corrupt turns_at_last_compute (TypeError, swallowed) must not
    # suppress the gist-arm recovery.
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    calls = []
    _patch_spawn(monkeypatch, calls)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    cache.save_cache("s1", {"score": 42, "label": "v1", "ts": _iso(now - 60),
                            "turns_at_last_compute": "nine"})
    cache.save_turns("s1", 6)
    statusline.render({"session_id": "s1", "transcript_path": "/t"}, now=now)
    assert calls == ["s1"]


def test_peek_recompute_when_turn_count_behind_cache(tmp_path, monkeypatch):
    # Lost/reset .turns with a surviving .json: the inconsistent state must
    # trigger a recompute, and the stale open loop must render dim — not
    # read as maximally fresh because 1 > 40 is False.
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    calls = []
    _patch_spawn(monkeypatch, calls)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    cache.save_cache("s1", {"score": 10, "gist": "parser work",
                            "open_loop": "pick a name",
                            "ts": _iso(now - 3 * 86_400),
                            "turns_at_last_compute": 40})
    cache.save_turns("s1", 1)
    out = statusline.render({"session_id": "s1", "transcript_path": "/t"}, now=now)
    assert calls == ["s1"]
    assert DIM + "⊙ your turn: pick a name" + RESET in out


def test_peek_no_recompute_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    calls = []
    _patch_spawn(monkeypatch, calls)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    cache.save_cache("s1", {"score": 10, "gist": "parser work",
                            "ts": _iso(now - 60), "turns_at_last_compute": 6})
    cache.save_turns("s1", 6)
    statusline.render({"session_id": "s1", "transcript_path": "/t"}, now=now)
    assert calls == []


def test_no_recompute_in_normal_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    calls = []
    _patch_spawn(monkeypatch, calls)
    cache.save_turns("s1", 8)   # stale, but no peek file → never spawn
    statusline.render({"session_id": "s1", "transcript_path": "/t"})
    assert calls == []


def test_render_survives_spawn_path_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    from whereami import drift

    def boom(*args, **kwargs):
        raise RuntimeError("marker create failed")

    monkeypatch.setattr(drift, "maybe_spawn_compute", boom)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    cache.save_turns("s1", 3)
    out = statusline.render({"session_id": "s1", "transcript_path": "/t"}, now=now)
    assert isinstance(out, str) and out   # render still returned a panel


def test_held_peek_no_goal_session_spawns_each_tick_no_llm(tmp_path, monkeypatch):
    # Pins the spec's accepted residual: a no-goal session early-returns
    # (unlinking the marker), so a held-open peek re-spawns once per refresh
    # tick — with ZERO LLM calls (the early return precedes the model call).
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    from whereami import drift
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    llm_calls = []
    monkeypatch.setattr(drift, "_run_claude",
                        lambda prompt: llm_calls.append(prompt) or "")
    spawns = []

    def sync_spawn(sid, path):
        spawns.append(sid)
        drift._compute_entry(sid, path)   # run the detached child inline

    monkeypatch.setattr(drift, "_spawn_compute", sync_spawn)
    now = 10_000.0
    _touch_peek(tmp_path, now)
    for tick in range(3):
        statusline.render({"session_id": "s1", "transcript_path": str(p)},
                          now=now + tick * 3)
    assert spawns == ["s1", "s1", "s1"]   # one spawn per tick
    assert llm_calls == []                # zero LLM calls
