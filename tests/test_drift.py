import io
import json
import os
import sys
from datetime import datetime, timedelta

from whereami import cache, drift


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


GOOD_REPLY = ('{"score": 58, "gist": "CI retry backoff logic", '
              '"open_loop": "choose a backoff strategy", "goal": "reorientation tool"}')


def _transcript(tmp_path, assistant_text="want me to proceed?"):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join([
        json.dumps({"type": "user",
                    "message": {"role": "user", "content": "build a parser"}}),
        json.dumps({"type": "assistant",
                    "message": {"role": "assistant", "content": assistant_text}}),
        json.dumps({"type": "user",
                    "message": {"role": "user", "content": "now fix deploy"}}),
    ]) + "\n")
    return p


def _run_hook_with(monkeypatch, payload, spawned):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(drift, "_spawn_compute",
                        lambda sid, path: spawned.append((sid, path)))
    try:
        drift.run_hook()
    except SystemExit:
        pass
    try:   # simulate the detached child finishing: it unlinks the marker
        os.unlink(str(cache.marker_path(payload["session_id"])))
    except OSError:
        pass


def test_compute_writes_v2_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    p = _transcript(tmp_path)
    cache.save_turns("s1", 6)
    drift.compute("s1", str(p), runner=lambda prompt: GOOD_REPLY)
    out = cache.load_cache("s1")
    assert out["score"] == 58
    assert out["gist"] == "CI retry backoff logic"
    assert out["open_loop"] == "choose a backoff strategy"
    assert out["goal"] == "reorientation tool"
    assert out["turns_at_last_compute"] == 6
    assert out["opening_goal"]
    assert out["ts"]


def test_compute_prompt_uses_tail_700_of_assistant_message(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    big = "A" * 300 + "B" * 700
    p = _transcript(tmp_path, assistant_text=big)
    prompts = []

    def runner(prompt):
        prompts.append(prompt)
        return GOOD_REPLY

    drift.compute("s1", str(p), runner=runner)
    assert "B" * 700 in prompts[0]   # the tail survives whole
    assert "AB" not in prompts[0]    # the head (and the head/tail seam) is gone


def test_compute_goal_keep_first_and_prompt_drops_goal(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    p = _transcript(tmp_path)
    prompts = []

    def recording(reply):
        def runner(prompt):
            prompts.append(prompt)
            return reply
        return runner

    drift.compute("s1", str(p), runner=recording(GOOD_REPLY))
    drift.compute("s1", str(p), runner=recording(
        '{"score": 5, "gist": "deploy fixes", "open_loop": "", '
        '"goal": "DIFFERENT GOAL"}'))
    out = cache.load_cache("s1")
    assert out["goal"] == "reorientation tool"   # keep-first: never overwritten
    assert '"goal"' in prompts[0]
    assert '"goal"' not in prompts[1]            # re-asking is pure token waste


def test_compute_continuity_feeds_previous_gist(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    p = _transcript(tmp_path)
    drift.compute("s1", str(p), runner=lambda prompt: GOOD_REPLY)
    prompts = []

    def runner(prompt):
        prompts.append(prompt)
        return GOOD_REPLY

    drift.compute("s1", str(p), runner=runner)
    assert 'Previous gist: "CI retry backoff logic"' in prompts[0]


def test_compute_parse_failure_writes_only_last_failure_ts(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    p = _transcript(tmp_path)
    cache.save_turns("s1", 9)
    good = {"score": 12, "gist": "old gist", "open_loop": "", "goal": "g",
            "opening_goal": "build a parser", "ts": "2026-06-09T10:00:00-07:00",
            "turns_at_last_compute": 3}
    cache.save_cache("s1", dict(good))
    drift.compute("s1", str(p), runner=lambda prompt: "I refuse to answer in JSON")
    out = cache.load_cache("s1")
    assert out.pop("last_failure_ts")
    assert out == good   # every other field preserved; ts NOT advanced


def test_compute_drops_dead_v1_fields_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    p = _transcript(tmp_path)
    cache.save_cache("s1", {"score": 1, "label": "old label", "turns_seen": 40,
                            "turns_at_last_compute": 40, "ts": "x",
                            "opening_goal": "build a parser"})
    drift.compute("s1", str(p), runner=lambda prompt: GOOD_REPLY)
    out = cache.load_cache("s1")
    assert "label" not in out        # the label field dies; gist replaces it
    assert "turns_seen" not in out   # the counter lives in .turns now


def test_turns_landing_during_slow_compute_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    p = _transcript(tmp_path)
    cache.save_turns("s1", 6)

    def slow_runner(prompt):
        cache.increment_turns("s1")   # two Stop hooks land mid-LLM-call
        cache.increment_turns("s1")
        return GOOD_REPLY

    drift.compute("s1", str(p), runner=slow_runner)
    assert cache.load_turns("s1") == 8   # compute never writes .turns
    assert cache.load_cache("s1")["turns_at_last_compute"] == 6  # read at start
    # → the renderer's staleness trigger (8 > 6) stays TRUE after the save:
    #   self-correcting (one extra guard-absorbed compute), not self-suppressing.


def test_hook_increments_turns_file_each_call(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    spawned = []
    payload = {"session_id": "s1", "transcript_path": str(tmp_path / "t.jsonl")}
    _run_hook_with(monkeypatch, payload, spawned)
    _run_hook_with(monkeypatch, payload, spawned)
    assert cache.load_turns("s1") == 2
    assert cache.load_cache("s1") == {}   # the hook never touches the json file


def test_hook_spawns_first_time_then_every_sixth(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    spawned = []
    payload = {"session_id": "s1", "transcript_path": str(tmp_path / "t.jsonl")}
    cache.save_turns("s1", 1)
    cache.save_cache("s1", {"ts": "2026-06-09T10:00:00-07:00",
                            "gist": "parser work", "turns_at_last_compute": 1})
    for _ in range(6):
        _run_hook_with(monkeypatch, payload, spawned)
    # turns go 2..7; due when 7 - 1 >= 6 → exactly once
    assert len(spawned) == 1


def test_hook_spawns_when_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    spawned = []
    _run_hook_with(monkeypatch, {"session_id": "s1",
                                 "transcript_path": str(tmp_path / "t.jsonl")}, spawned)
    assert len(spawned) == 1   # no ts → due immediately


def test_hook_due_via_gist_arm_on_v1_cache(tmp_path, monkeypatch):
    # Resumed v1 session: ts present, gist absent, turn-delta hugely negative.
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    spawned = []
    cache.save_cache("s1", {"score": 42, "label": "old",
                            "ts": "2026-06-09T10:00:00-07:00",
                            "turns_seen": 40, "turns_at_last_compute": 40})
    payload = {"session_id": "s1", "transcript_path": str(tmp_path / "t.jsonl")}
    _run_hook_with(monkeypatch, payload, spawned)   # .turns becomes 1; delta -39
    assert len(spawned) == 1                        # due via the gist-missing arm


def test_hook_goes_through_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"session_id": "s1", "transcript_path": "/t"})))
    monkeypatch.setattr(drift, "_spawn_compute", lambda sid, path: None)
    try:
        drift.run_hook()
    except SystemExit:
        pass
    assert cache.marker_path("s1").exists()   # spawn went through the marker guard


def test_hook_survives_non_dict_cache_json(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    (tmp_path / "s1.json").write_text("[1, 2, 3]")   # hand-edit gone wrong
    spawned = []
    _run_hook_with(monkeypatch, {"session_id": "s1",
                                 "transcript_path": str(tmp_path / "t.jsonl")}, spawned)
    assert cache.load_turns("s1") == 1   # hook completed; no raise
    assert len(spawned) == 1             # treated as no-cache → due


def test_persistent_parse_failure_suppresses_hook_spawns(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    p = _transcript(tmp_path)
    # A failing compute stamps last_failure_ts (real wall clock)…
    drift.compute("s1", str(p), runner=lambda prompt: "junk")
    assert "last_failure_ts" in cache.load_cache("s1")
    # …after which due hooks stop spawning while the backoff is fresh.
    spawned = []
    payload = {"session_id": "s1", "transcript_path": str(p)}
    for _ in range(3):
        _run_hook_with(monkeypatch, payload, spawned)
    assert spawned == []
    # Retry resumes once the failure ages past FAILURE_BACKOFF.
    data = cache.load_cache("s1")
    data["last_failure_ts"] = (datetime.now().astimezone()
                               - timedelta(seconds=drift.FAILURE_BACKOFF + 60)
                               ).isoformat(timespec="seconds")
    cache.save_cache("s1", data)
    _run_hook_with(monkeypatch, payload, spawned)
    assert len(spawned) == 1
