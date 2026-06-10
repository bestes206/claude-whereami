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


def test_hand_edited_goal_during_slow_compute_sticks(tmp_path, monkeypatch):
    # The spec's escape hatch: "hand-edit the goal field — it is never
    # overwritten, so the edit sticks." The compute's dict predates a ≤60s
    # LLM call, so it must re-read the goal before saving — on the success
    # path AND the parse-failure path.
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    p = _transcript(tmp_path)

    def editing_runner(edit, reply):
        def runner(prompt):
            data = cache.load_cache("s1")
            data["goal"] = edit
            cache.save_cache("s1", data)
            return reply
        return runner

    cache.save_cache("s1", {"goal": "model goal", "score": 1, "gist": "old",
                            "opening_goal": "build a parser",
                            "ts": "2026-06-09T10:00:00-07:00",
                            "turns_at_last_compute": 0})
    drift.compute("s1", str(p), runner=editing_runner(
        "FIRST EDIT", '{"score": 5, "gist": "deploy fixes", "open_loop": ""}'))
    assert cache.load_cache("s1")["goal"] == "FIRST EDIT"

    drift.compute("s1", str(p), runner=editing_runner(
        "SECOND EDIT", "not json at all"))
    assert cache.load_cache("s1")["goal"] == "SECOND EDIT"


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


def test_orient_bool_or_infinite_score_is_parse_failure():
    # int(True) == 1 would cache "yes, drifted" as a near-best score; and
    # json.loads accepts Infinity, where int() raises OverflowError — both
    # must be parse failures, not crashes or inverted signals.
    assert drift.orient("g", "r", "t", None, False,
                        runner=lambda p: '{"score": true, "gist": "auth work"}') is None
    assert drift.orient("g", "r", "t", None, False,
                        runner=lambda p: '{"score": Infinity, "gist": "auth work"}') is None
    assert drift.orient("g", "r", "t", None, False,
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


def test_cli_version_returns_stripped_stdout(monkeypatch):
    class FakeProc:
        stdout = "2.1.172 (Claude Code)\n"

    monkeypatch.setattr(drift.subprocess, "run", lambda *a, **k: FakeProc())
    assert drift._cli_version() == "2.1.172 (Claude Code)"


def test_cli_version_empty_on_error(monkeypatch):
    def boom(*a, **k):
        raise OSError()

    monkeypatch.setattr(drift.subprocess, "run", boom)
    assert drift._cli_version() == ""


def test_build_argv_unstripped_is_todays_behavior():
    argv = drift._build_argv("PROMPT", stripped=False)
    assert argv == ["claude", "-p", "PROMPT",
                    "--model", drift.HAIKU_MODEL, "--output-format", "json"]


def test_build_argv_stripped_adds_all_six_flags():
    argv = drift._build_argv("PROMPT", stripped=True)
    assert argv[:7] == ["claude", "-p", "PROMPT", "--model", drift.HAIKU_MODEL,
                        "--output-format", "json"]
    assert "--exclude-dynamic-system-prompt-sections" in argv
    assert "--strict-mcp-config" in argv
    # empty-string args are load-bearing: --tools "" drops ~11K of tool schemas
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    sp = argv[argv.index("--system-prompt") + 1]
    assert "JSON-only classifier" in sp


def test_probe_json_ok_accepts_object_and_fences():
    assert drift._probe_json_ok('{"answer": 59}') is True
    assert drift._probe_json_ok('```json\n{"answer": 59}\n```') is True
    assert drift._probe_json_ok('here: {"answer": 59} done') is True


def test_probe_json_ok_rejects_non_object():
    assert drift._probe_json_ok("sorry, no JSON") is False
    assert drift._probe_json_ok(None) is False
    assert drift._probe_json_ok("[1, 2]") is False   # array is not an object


def test_output_tokens_reads_usage():
    assert drift._output_tokens({"usage": {"output_tokens": 42}}) == 42
    assert drift._output_tokens({}) is None
    assert drift._output_tokens({"usage": {}}) is None
    assert drift._output_tokens({"usage": {"output_tokens": True}}) is None  # bool


def test_stripped_probe_ok_requires_json_AND_low_tokens():
    ok = {"result": '{"answer": 59}', "usage": {"output_tokens": 20}}
    assert drift._stripped_probe_ok(ok) is True
    # thinking still on → output spikes → reject even though JSON parses
    hot = {"result": '{"answer": 59}', "usage": {"output_tokens": 2025}}
    assert drift._stripped_probe_ok(hot) is False
    # no usage → can't confirm thinking-off → reject
    assert drift._stripped_probe_ok({"result": '{"answer": 59}'}) is False
    # not parseable → reject
    assert drift._stripped_probe_ok(
        {"result": "thinking out loud", "usage": {"output_tokens": 5}}) is False


def test_stripped_supported_cache_hit_skips_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(drift, "_cli_version", lambda: "2.1.172")
    cache.save_caps({"cli_version": "2.1.172", "stripped_ok": True})

    def no_probe(*a, **k):
        raise AssertionError("must not probe on a cache hit")

    monkeypatch.setattr(drift, "_invoke", no_probe)
    assert drift._stripped_supported() is True


def test_stripped_supported_cache_hit_false(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(drift, "_cli_version", lambda: "1.0.0")
    cache.save_caps({"cli_version": "1.0.0", "stripped_ok": False})

    def no_probe(*a, **k):
        raise AssertionError("must not probe on a cache hit")

    monkeypatch.setattr(drift, "_invoke", no_probe)
    assert drift._stripped_supported() is False


def test_stripped_supported_probes_and_caches_true(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(drift, "_cli_version", lambda: "2.1.172")
    monkeypatch.setattr(drift, "_invoke",
                        lambda args, env=None: {"result": '{"answer": 59}',
                                                "usage": {"output_tokens": 20}})
    assert drift._stripped_supported() is True
    assert cache.load_caps() == {"cli_version": "2.1.172", "stripped_ok": True}


def test_stripped_supported_flags_unsupported_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(drift, "_cli_version", lambda: "1.0.0")
    seq = iter([
        {},  # stripped probe fails (old CLI rejects the flags)
        {"result": '{"answer": 59}', "usage": {"output_tokens": 1500}},  # unstripped works
    ])
    monkeypatch.setattr(drift, "_invoke", lambda *a, **k: next(seq))
    assert drift._stripped_supported() is False
    assert cache.load_caps() == {"cli_version": "1.0.0", "stripped_ok": False}


def test_stripped_supported_transient_failure_backs_off(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(drift, "_cli_version", lambda: "2.1.172")
    calls = []

    def failing(*a, **k):
        calls.append(a)
        return {}

    monkeypatch.setattr(drift, "_invoke", failing)
    assert drift._stripped_supported() is False
    assert len(calls) == 2   # stripped + unstripped disambiguation
    caps = cache.load_caps()
    assert caps.get("probed_at") and "stripped_ok" not in caps
    # second compute within FAILURE_BACKOFF → reuse, do NOT re-probe
    calls.clear()
    assert drift._stripped_supported() is False
    assert calls == []


def test_stripped_supported_reprobes_after_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(drift, "_cli_version", lambda: "2.1.172")
    stale = (datetime.now().astimezone()
             - timedelta(seconds=drift.FAILURE_BACKOFF + 60)
             ).isoformat(timespec="seconds")
    cache.save_caps({"cli_version": "2.1.172", "probed_at": stale})
    monkeypatch.setattr(drift, "_invoke",
                        lambda *a, **k: {"result": '{"answer": 1}',
                                         "usage": {"output_tokens": 10}})
    assert drift._stripped_supported() is True   # cooldown elapsed → re-probe


def test_stripped_supported_reprobes_on_version_change(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache.save_caps({"cli_version": "OLD", "stripped_ok": True})
    monkeypatch.setattr(drift, "_cli_version", lambda: "NEW")
    monkeypatch.setattr(drift, "_invoke", lambda *a, **k: {})   # both calls fail
    assert drift._stripped_supported() is False
    assert cache.load_caps().get("cli_version") == "NEW"


def test_run_claude_uses_stripped_when_supported(monkeypatch):
    monkeypatch.setattr(drift, "_stripped_supported", lambda: True)
    captured = {}

    def fake_invoke(args, env=None):
        captured["args"] = args
        captured["env"] = env
        return {"result": '{"score": 1, "gist": "x"}'}

    monkeypatch.setattr(drift, "_invoke", fake_invoke)
    assert drift._run_claude("PROMPT") == '{"score": 1, "gist": "x"}'
    assert "--tools" in captured["args"]
    assert captured["env"] == {"MAX_THINKING_TOKENS": "0"}


def test_run_claude_uses_unstripped_when_unsupported(monkeypatch):
    monkeypatch.setattr(drift, "_stripped_supported", lambda: False)
    captured = {}

    def fake_invoke(args, env=None):
        captured["args"] = args
        captured["env"] = env
        return {"result": "reply"}

    monkeypatch.setattr(drift, "_invoke", fake_invoke)
    assert drift._run_claude("PROMPT") == "reply"
    assert captured["args"] == ["claude", "-p", "PROMPT", "--model",
                                drift.HAIKU_MODEL, "--output-format", "json"]
    assert captured["env"] is None   # today's behavior verbatim: no env override


def test_run_claude_non_str_result_is_empty(monkeypatch):
    monkeypatch.setattr(drift, "_stripped_supported", lambda: True)
    monkeypatch.setattr(drift, "_invoke", lambda *a, **k: {"result": {"x": 1}})
    assert drift._run_claude("PROMPT") == ""


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
