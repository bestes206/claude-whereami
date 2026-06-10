from whereami import drift


def test_score_drift_parses_json():
    score, label = drift.score_drift(
        "build a parser", "fix the CI deploy",
        runner=lambda prompt: '{"score": 72, "label": "drifted to deploy config"}')
    assert score == 72
    assert label == "drifted to deploy config"


def test_score_drift_handles_junk():
    score, label = drift.score_drift("x", "y", runner=lambda prompt: "sorry I cannot help")
    assert score == 0
    assert label == ""


def test_score_drift_clamps_out_of_range():
    score, label = drift.score_drift(
        "x", "y", runner=lambda prompt: '{"score": 250, "label": "way off"}')
    assert score == 100
    assert label == "way off"


def test_score_drift_runner_failure_is_safe():
    # a runner that returns '' (e.g. claude not found) must not raise
    score, label = drift.score_drift("x", "y", runner=lambda prompt: "")
    assert score == 0
    assert label == ""


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

    monkeypatch.setattr(drift, "score_drift", lambda g, r, runner=None: (55, "on deploy"))
    drift.compute("s1", str(p))

    out = cache.load_cache("s1")
    assert out["score"] == 55
    assert out["label"] == "on deploy"
    assert out["turns_at_last_compute"] == 6
    assert out["opening_goal"]
    assert out["ts"]


# tests/test_drift.py  (append)
import io
import json
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


def test_orient_parses_full_contract():
    out = drift.orient(
        "goal", "recent", "tail", None, True,
        runner=lambda p: '{"score": 58, "gist": "CI retry backoff logic", '
                         '"open_loop": "choose a backoff strategy", '
                         '"goal": "reorientation tool"}')
    assert out == {"score": 58, "gist": "CI retry backoff logic",
                   "open_loop": "choose a backoff strategy",
                   "goal": "reorientation tool"}


def test_orient_junk_reply_is_parse_failure():
    assert drift.orient("g", "r", "t", None, True, runner=lambda p: "sorry, no") is None
    assert drift.orient("g", "r", "t", None, True, runner=lambda p: "") is None


def test_orient_missing_or_blank_gist_is_parse_failure():
    assert drift.orient("g", "r", "t", None, False,
                        runner=lambda p: '{"score": 10}') is None
    assert drift.orient("g", "r", "t", None, False,
                        runner=lambda p: '{"score": 10, "gist": "  "}') is None


def test_orient_bad_score_is_parse_failure():
    assert drift.orient("g", "r", "t", None, False,
                        runner=lambda p: '{"score": "high", "gist": "parser work"}') is None


def test_orient_score_clamped():
    out = drift.orient("g", "r", "t", None, False,
                       runner=lambda p: '{"score": 250, "gist": "parser work", '
                                        '"open_loop": ""}')
    assert out["score"] == 100


def test_orient_bad_or_missing_open_loop_coerced_to_empty():
    out = drift.orient("g", "r", "t", None, False,
                       runner=lambda p: '{"score": 5, "gist": "parser work", '
                                        '"open_loop": 42}')
    assert out["open_loop"] == ""
    out = drift.orient("g", "r", "t", None, False,
                       runner=lambda p: '{"score": 5, "gist": "parser work"}')
    assert out["open_loop"] == ""


def test_orient_missing_goal_ignored():
    out = drift.orient("g", "r", "t", None, True,
                       runner=lambda p: '{"score": 5, "gist": "parser work", '
                                        '"open_loop": ""}')
    assert "goal" not in out


def test_build_prompt_goal_field_only_when_wanted():
    with_goal = drift.build_prompt("g", "r", "t", None, True)
    without = drift.build_prompt("g", "r", "t", None, False)
    assert '"goal"' in with_goal
    assert '"goal"' not in without


def test_build_prompt_continuity_includes_previous_gist():
    p = drift.build_prompt("g", "r", "t", "CI retry backoff logic", False)
    assert 'Previous gist: "CI retry backoff logic"' in p
    assert "keep it unless the focus genuinely changed" in p
    assert "Previous gist" not in drift.build_prompt("g", "r", "t", None, False)


def test_build_prompt_includes_assistant_tail_and_hardening():
    p = drift.build_prompt("g", "r", "the ask is at the end", None, False)
    assert "the ask is at the end" in p
    assert "never invent an ask" in p   # open_loop must be "" when nothing is awaited
    assert "forbidden" in p             # generic gist words banned
    assert "USER" in p                  # describe the user's intent, not agent busywork
