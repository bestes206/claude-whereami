import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from whereami import cache, drift


def test_orient_parses_full_contract():
    out = drift.orient(
        "goal", "recent", "tail", None,
        runner=lambda p: '{"score": 58, "gist": "CI retry backoff logic", '
                         '"open_loop": "choose a backoff strategy", '
                         '"goal": "reorientation tool"}')
    # Extra fields a model adds (open_loop/goal from the pre-0.4 contract) are ignored.
    assert out == {"score": 58, "gist": "CI retry backoff logic"}


def test_orient_junk_reply_is_parse_failure():
    assert drift.orient("g", "r", "t", None, runner=lambda p: "sorry, no") is None
    assert drift.orient("g", "r", "t", None, runner=lambda p: "") is None


def test_orient_missing_or_blank_gist_is_parse_failure():
    assert drift.orient("g", "r", "t", None,
                        runner=lambda p: '{"score": 10}') is None
    assert drift.orient("g", "r", "t", None,
                        runner=lambda p: '{"score": 10, "gist": "  "}') is None


def test_orient_bad_score_is_parse_failure():
    assert drift.orient("g", "r", "t", None,
                        runner=lambda p: '{"score": "high", "gist": "parser work"}') is None


def test_orient_score_clamped():
    out = drift.orient("g", "r", "t", None,
                       runner=lambda p: '{"score": 250, "gist": "parser work", '
                                        '"open_loop": ""}')
    assert out["score"] == 100


def test_build_prompt_continuity_includes_previous_gist():
    p = drift.build_prompt("g", "r", "t", "CI retry backoff logic")
    assert 'Previous gist: "CI retry backoff logic"' in p
    assert "keep it unless the focus genuinely changed" in p
    assert "Previous gist" not in drift.build_prompt("g", "r", "t", None)


def test_build_prompt_includes_assistant_tail_and_hardening():
    p = drift.build_prompt("g", "r", "the ask is at the end", None)
    assert "the ask is at the end" in p
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
    # An interactive session always has a transcript on disk by its first Stop;
    # a missing one now means a headless --no-session-persistence run.
    path = payload.get("transcript_path")
    if path and not os.path.exists(path):
        open(path, "w").close()
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
    assert "open_loop" not in out and "goal" not in out
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
                            "opening_goal": "build a parser",
                            "open_loop": "pick one", "goal": "old goal"})
    drift.compute("s1", str(p), runner=lambda prompt: GOOD_REPLY)
    out = cache.load_cache("s1")
    assert "label" not in out        # the label field dies; gist replaces it
    assert "turns_seen" not in out   # the counter lives in .turns now
    assert "open_loop" not in out and "goal" not in out   # peek-only, gone in 0.4


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


def test_orient_bool_or_infinite_score_is_parse_failure():
    # int(True) == 1 would cache "yes, drifted" as a near-best score; and
    # json.loads accepts Infinity, where int() raises OverflowError — both
    # must be parse failures, not crashes or inverted signals.
    assert drift.orient("g", "r", "t", None,
                        runner=lambda p: '{"score": true, "gist": "auth work"}') is None
    assert drift.orient("g", "r", "t", None,
                        runner=lambda p: '{"score": Infinity, "gist": "auth work"}') is None
    assert drift.orient("g", "r", "t", None,
                        runner=lambda p: '{"score": NaN, "gist": "auth work"}') is None


def test_run_claude_tolerates_shape_changed_envelope(monkeypatch):
    # Risk E: the -p JSON envelope is an undocumented contract. A shape
    # change must degrade to "" (→ parse failure → last_failure_ts →
    # backoff), never raise out of the compute child.
    class FakeProc:
        def __init__(self, stdout):
            self.stdout = stdout

    for stdout in ("[1, 2]",                    # non-dict envelope
                   "null",
                   '{"result": {"score": 5}}',  # non-str result
                   '{"result": null}'):
        monkeypatch.setattr(drift.subprocess, "run",
                            lambda *a, stdout=stdout, **k: FakeProc(stdout))
        assert drift._run_claude("prompt") == ""


def test_hook_exits_cleanly_on_non_object_payload(tmp_path, monkeypatch):
    # `null`/lists are valid JSON, so the ValueError guard doesn't fire;
    # the hook must exit 0 silently, as statusline.main survives the same.
    import pytest

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    for payload in ("null", "[1, 2]", '"a string"'):
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        with pytest.raises(SystemExit) as exc:
            drift.run_hook()
        assert exc.value.code == 0


def test_hook_due_tolerates_corrupt_talc():
    # Spec-sanctioned hand-edits can corrupt turns_at_last_compute; the Stop
    # hook must degrade (counter reads as 0), not crash every turn.
    data = {"ts": "2026-06-09T10:00:00-07:00", "gist": "parser work",
            "turns_at_last_compute": "nine"}
    assert drift.hook_due(data, 7) is True
    assert drift.hook_due(data, 3) is False  # still throttles from 0


def test_hook_due_when_turn_count_behind_cache():
    # A reset/lost .turns file (json survives, talc=40, count restarts at 1)
    # is inconsistent state: days-old data would otherwise render as fresh
    # for ~45 turns. A negative delta is due, not fresh.
    data = {"ts": "2026-06-09T10:00:00-07:00", "gist": "parser work",
            "turns_at_last_compute": 40}
    assert drift.hook_due(data, 1) is True


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
    t = tmp_path / "t.jsonl"
    t.write_text("")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"session_id": "s1", "transcript_path": str(t)})))
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


def test_invoke_returns_full_envelope_and_passes_args(monkeypatch):
    captured = {}

    class FakeProc:
        stdout = '{"result": "hi", "usage": {"output_tokens": 7}}'

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(drift.subprocess, "run", fake_run)
    env = drift._invoke(["claude", "-p", "x"], {"MAX_THINKING_TOKENS": "0"})
    assert env == {"result": "hi", "usage": {"output_tokens": 7}}
    assert captured["args"] == ["claude", "-p", "x"]
    assert captured["env"]["MAX_THINKING_TOKENS"] == "0"
    assert "PATH" in captured["env"]   # merged OVER os.environ, not replaced


def test_invoke_none_env_inherits_parent(monkeypatch):
    captured = {}

    class FakeProc:
        stdout = '{"result": "ok"}'

    def fake_run(args, **kwargs):
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(drift.subprocess, "run", fake_run)
    drift._invoke(["claude"], None)
    assert captured["env"] is None   # today's behavior: inherit verbatim


def test_invoke_degrades_to_empty_dict(monkeypatch):
    class FakeProc:
        def __init__(self, stdout):
            self.stdout = stdout

    for stdout in ("[1, 2]", "null", "not json", ""):
        monkeypatch.setattr(drift.subprocess, "run",
                            lambda *a, stdout=stdout, **k: FakeProc(stdout))
        assert drift._invoke(["claude"], None) == {}


def test_invoke_subprocess_error_is_empty(monkeypatch):
    def boom(*a, **k):
        raise OSError("no claude")

    monkeypatch.setattr(drift.subprocess, "run", boom)
    assert drift._invoke(["claude"], None) == {}


def test_run_claude_non_str_result_is_empty(monkeypatch):
    monkeypatch.setattr(drift, "_invoke", lambda *a, **k: {"result": {"x": 1}})
    assert drift._run_claude("PROMPT") == ""


def _ts(obj, timestamp):
    obj = dict(obj)
    obj["timestamp"] = timestamp
    return json.dumps(obj)


def _idle_transcript(tmp_path, gap_seconds):
    base = datetime(2026, 6, 10, 21, 0, 0, tzinfo=timezone.utc)
    later = base + timedelta(seconds=gap_seconds)
    z = lambda d: d.isoformat().replace("+00:00", "Z")   # exercise Z-normalization
    p = tmp_path / "idle.jsonl"
    p.write_text("\n".join([
        _ts({"type": "user", "message": {"role": "user", "content": "first ask"}}, z(base)),
        _ts({"type": "assistant", "message": {"role": "assistant", "content": "ok"}}, z(base)),
        _ts({"type": "user", "message": {"role": "user", "content": "second ask"}}, z(later)),
    ]) + "\n")
    return p


def test_returned_from_idle_true_when_gap_exceeds(tmp_path):
    p = _idle_transcript(tmp_path, gap_seconds=20 * 60)
    assert drift.returned_from_idle(str(p), threshold_seconds=10 * 60) is True


def test_returned_from_idle_false_when_gap_small(tmp_path):
    p = _idle_transcript(tmp_path, gap_seconds=60)
    assert drift.returned_from_idle(str(p), threshold_seconds=10 * 60) is False


def test_returned_from_idle_false_with_fewer_than_two_human_turns(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(_ts({"type": "user", "message": {"role": "user",
                  "content": "only ask"}}, "2026-06-10T21:00:00Z") + "\n")
    assert drift.returned_from_idle(str(p), 600) is False


def test_returned_from_idle_false_on_missing_file(tmp_path):
    assert drift.returned_from_idle(str(tmp_path / "none.jsonl"), 600) is False


def test_long_agent_turn_trips_idle_gap(tmp_path):
    # The human-to-human gap includes the previous agent turn's runtime: a long
    # autonomous run crosses the threshold with no human idleness. Benign by
    # design — a gist from before a long run is exactly the kind that's stale.
    p = tmp_path / "t.jsonl"
    lines = [
        _ts({"type": "user", "message": {"role": "user", "content": "go"}},
            "2026-06-10T21:00:00Z"),
        _ts({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "..."}]}}, "2026-06-10T21:04:00Z"),
        _ts({"type": "user", "message": {"role": "user", "content": "next"}},
            "2026-06-10T21:08:00Z"),
    ]
    p.write_text("\n".join(lines) + "\n")
    assert drift.returned_from_idle(str(p), threshold_seconds=10 * 60) is False
    assert drift.returned_from_idle(str(p), threshold_seconds=5 * 60) is True


def test_read_idle_threshold_default_and_overrides(monkeypatch):
    monkeypatch.delenv("WHEREAMI_IDLE_MIN", raising=False)
    assert drift._read_idle_threshold() == 600
    monkeypatch.setenv("WHEREAMI_IDLE_MIN", "5")
    assert drift._read_idle_threshold() == 300
    monkeypatch.setenv("WHEREAMI_IDLE_MIN", "0")
    assert drift._read_idle_threshold() == 600    # non-positive → default
    monkeypatch.setenv("WHEREAMI_IDLE_MIN", "-3")
    assert drift._read_idle_threshold() == 600
    monkeypatch.setenv("WHEREAMI_IDLE_MIN", "garbage")
    assert drift._read_idle_threshold() == 600


def test_hook_due_idle_returned_forces_due():
    data = {"ts": "2026-06-09T10:00:00-07:00", "gist": "parser work",
            "turns_at_last_compute": 5}
    assert drift.hook_due(data, 6) is False                       # delta 1 < 6
    assert drift.hook_due(data, 6, idle_returned=True) is True    # idle forces due


def test_hook_due_default_idle_param_keeps_existing_behavior():
    data = {"ts": "x", "gist": "g", "turns_at_last_compute": 5}
    assert drift.hook_due(data, 6) is False   # 3-arg call still valid (no churn)


def test_hook_spawns_on_idle_return_below_throttle(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    p = _idle_transcript(tmp_path, gap_seconds=20 * 60)   # > default 10 min
    cache.save_turns("s1", 5)
    cache.save_cache("s1", {"ts": "2026-06-09T10:00:00-07:00", "gist": "g",
                            "turns_at_last_compute": 5})
    spawned = []
    payload = {"session_id": "s1", "transcript_path": str(p)}
    _run_hook_with(monkeypatch, payload, spawned)   # turns 5→6, cadence not due
    assert len(spawned) == 1   # spawned via the idle-return arm


def test_hook_no_idle_spawn_when_gap_small(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    p = _idle_transcript(tmp_path, gap_seconds=60)
    cache.save_turns("s1", 5)
    cache.save_cache("s1", {"ts": "2026-06-09T10:00:00-07:00", "gist": "g",
                            "turns_at_last_compute": 5})
    spawned = []
    payload = {"session_id": "s1", "transcript_path": str(p)}
    _run_hook_with(monkeypatch, payload, spawned)
    assert spawned == []   # small gap + cadence not due → no spawn


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


# --- slim-down (0.4.0) ------------------------------------------------------

def test_build_argv_always_stripped_and_never_persists_a_session():
    argv = drift._build_argv("PROMPT")
    assert argv[:7] == ["claude", "-p", "PROMPT", "--model", drift.HAIKU_MODEL,
                        "--output-format", "json"]
    # Each scoring call used to save its own transcript: 1,211 of them on disk.
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""


def test_run_claude_makes_exactly_one_call_with_thinking_off(monkeypatch):
    calls = []

    def fake_invoke(args, env=None):
        calls.append((args, env))
        return {"result": '{"score": 1, "gist": "x"}'}

    monkeypatch.setattr(drift, "_invoke", fake_invoke)
    assert drift._run_claude("PROMPT") == '{"score": 1, "gist": "x"}'
    assert len(calls) == 1                      # no capability probe, no fallback
    assert "--no-session-persistence" in calls[0][0]
    assert calls[0][1] == {"MAX_THINKING_TOKENS": "0"}


def test_hook_skips_headless_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"type": "user", "entrypoint": "sdk-cli",
                             "message": {"role": "user", "content": "run the job"}}) + "\n")
    spawned = []
    _run_hook_with(monkeypatch, {"session_id": "s1", "transcript_path": str(p)}, spawned)
    assert spawned == []
    assert cache.load_turns("s1") == 0          # leaves nothing behind


def test_hook_skips_session_without_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"session_id": "s1", "transcript_path": str(tmp_path / "gone.jsonl")})))
    spawned = []
    monkeypatch.setattr(drift, "_spawn_compute", lambda sid, path: spawned.append(sid))
    try:
        drift.run_hook()
    except SystemExit:
        pass
    assert spawned == [] and cache.load_turns("s1") == 0


def test_build_prompt_asks_only_for_score_and_gist():
    prompt = drift.build_prompt("goal", "recent", "tail", None)
    assert '"score"' in prompt and '"gist"' in prompt
    assert "open_loop" not in prompt and '"goal"' not in prompt
