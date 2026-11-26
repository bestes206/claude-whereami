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


def test_assistant_text_extracts_text_blocks():
    entry = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Here is the fix."}]}}
    assert transcript.assistant_text(entry) == "Here is the fix."


def test_assistant_text_string_content():
    entry = {"type": "assistant", "message": {"role": "assistant", "content": "plain answer"}}
    assert transcript.assistant_text(entry) == "plain answer"


def test_assistant_text_skips_user_sidechain_and_tooluse_only():
    assert transcript.assistant_text(
        {"type": "user", "message": {"role": "user", "content": "hi"}}) is None
    assert transcript.assistant_text(
        {"type": "assistant", "isSidechain": True,
         "message": {"role": "assistant", "content": "x"}}) is None
    tool_only = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Bash", "input": {}}]}}
    assert transcript.assistant_text(tool_only) is None


def test_last_assistant_text_reads_from_end(tmp_path):
    p = tmp_path / "t.jsonl"
    lines = [
        _line({"type": "assistant", "message": {"role": "assistant", "content": "old answer"}}),
        _line({"type": "user", "message": {"role": "user", "content": "next ask"}}),
        _line({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {}}]}}),
        _line({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "want me to proceed?"}]}}),
    ]
    p.write_text("\n".join(lines) + "\n")
    assert transcript.last_assistant_text(str(p)) == "want me to proceed?"


def test_last_assistant_text_missing_file_returns_none():
    assert transcript.last_assistant_text("/no/such/file.jsonl") is None


def _ts_line(obj, ts):
    obj = dict(obj)
    obj["timestamp"] = ts
    return json.dumps(obj)


def test_human_turn_timestamps_skips_tool_results(tmp_path):
    # type=="user" is overwhelmingly tool_result envelopes; filtering on that
    # alone would pick two adjacent tool-results seconds apart and idle would
    # never fire. Must filter via human_text() like the rest of the module.
    p = tmp_path / "t.jsonl"
    lines = [
        _ts_line({"type": "user", "message": {"role": "user", "content": "first ask"}},
                 "2026-06-10T21:00:00Z"),
        _ts_line({"type": "assistant", "message": {"role": "assistant", "content": "ok"}},
                 "2026-06-10T21:00:05Z"),
        _ts_line({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "out"}]}}, "2026-06-10T21:00:06Z"),
        _ts_line({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "out2"}]}}, "2026-06-10T21:00:07Z"),
        _ts_line({"type": "user", "message": {"role": "user", "content": "second ask"}},
                 "2026-06-10T21:20:00Z"),
    ]
    p.write_text("\n".join(lines) + "\n")
    stamps = transcript.human_turn_timestamps(str(p), n=2)
    assert len(stamps) == 2
    # most-recent-first → positive gap; 21:20 − 21:00, NOT the 1s tool-result gap
    assert (stamps[0] - stamps[1]).total_seconds() == 20 * 60


def test_human_turn_timestamps_skips_garbage_timestamps(tmp_path):
    p = tmp_path / "t.jsonl"
    lines = [
        _ts_line({"type": "user", "message": {"role": "user", "content": "a"}},
                 "2026-06-10T21:00:00Z"),
        _ts_line({"type": "user", "message": {"role": "user", "content": "b"}}, "nope"),
        _ts_line({"type": "user", "message": {"role": "user", "content": "c"}},
                 "2026-06-10T21:05:00Z"),
    ]
    p.write_text("\n".join(lines) + "\n")
    stamps = transcript.human_turn_timestamps(str(p), n=2)
    assert len(stamps) == 2   # 'b' (garbage ts) skipped; 'c' then 'a'


def test_human_turn_timestamps_missing_file_is_empty(tmp_path):
    assert transcript.human_turn_timestamps(str(tmp_path / "none.jsonl")) == []


# --- Real entry shapes seen in CLI 2.1.332 transcripts (2026-11-25) --------

def _user(content, **extra):
    entry = {"type": "user", "message": {"role": "user", "content": content}}
    entry.update(extra)
    return entry


def test_task_notification_with_origin_is_not_human():
    # The screenshot bug: a background-task notice took over line 2.
    entry = _user("<task-notification>\n<task-id>aeae91aec4fa910b1</task-id>\n"
                  "<output-file>/private/tmp/x.output</output-file>",
                  origin={"kind": "task-notification", "producer": "session-task"})
    assert transcript.human_text(entry) is None


def test_peer_origin_is_not_human():
    # Messages relayed from another Claude session.
    assert transcript.human_text(_user("status update", origin={"kind": "peer"})) is None


def test_human_origin_is_human():
    assert transcript.human_text(_user("yes go ahead", origin={"kind": "human"})) == "yes go ahead"


def test_task_notification_without_origin_is_not_human():
    # Older transcripts carry no origin field.
    assert transcript.human_text(_user("<task-notification>\n<task-id>b1</task-id>")) is None


def test_compact_summary_is_not_human():
    entry = _user("This session is being continued from a previous conversation...",
                  isCompactSummary=True, isVisibleInTranscriptOnly=True)
    assert transcript.human_text(entry) is None


def test_local_command_and_bash_output_are_not_human():
    for text in ("<local-command-stdout>Login successful</local-command-stdout>",
                 "<local-command-stderr>oops</local-command-stderr>",
                 "<bash-stdout>total 0</bash-stdout><bash-stderr></bash-stderr>"):
        assert transcript.human_text(_user(text)) is None, text


def test_bash_input_shows_as_bang_command():
    assert transcript.human_text(_user("<bash-input> ls -la</bash-input>")) == "! ls -la"


def _queued(prompt, origin=None, ts="2026-11-26T03:17:51.273Z"):
    att = {"type": "queued_command", "prompt": prompt, "commandMode": "prompt",
           "humanTurn": True, "timestamp": ts}
    if origin is not None:
        att["origin"] = origin
    return {"type": "attachment", "attachment": att, "timestamp": ts}


def test_mid_turn_queued_message_is_human():
    # Messages typed while Claude works are stored as attachments, not user entries.
    entry = _queued("here is the repo: https://github.com/x/y", origin={"kind": "human"})
    assert transcript.human_text(entry) == "here is the repo: https://github.com/x/y"


def test_mid_turn_queued_message_with_image_blocks():
    entry = _queued([{"type": "text", "text": "[Image #1] here is the screenshot"},
                     {"type": "image", "source": {"type": "base64", "data": "AAAA"}}],
                    origin={"kind": "human"})
    assert transcript.human_text(entry) == "[Image #1] here is the screenshot"


def test_queued_non_human_is_not_human():
    entry = _queued("<task-notification>x</task-notification>",
                    origin={"kind": "task-notification"})
    assert transcript.human_text(entry) is None


def test_other_attachments_are_not_human():
    assert transcript.human_text({"type": "attachment",
                                  "attachment": {"type": "todo", "content": "x"}}) is None


def _write(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_last_human_text_skips_trailing_notification(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [_user("the real ask", origin={"kind": "human"}),
               _user("<task-notification>x</task-notification>",
                     origin={"kind": "task-notification"})])
    assert transcript.last_human_text(str(p)) == "the real ask"


def test_last_human_text_finds_queued_message(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [_user("first ask", origin={"kind": "human"}),
               _queued("typed mid-turn", origin={"kind": "human"})])
    assert transcript.last_human_text(str(p)) == "typed mid-turn"


def test_last_human_text_reaches_past_long_tool_runs(tmp_path):
    # 26% of real transcript positions had the last human message >200 lines back.
    tool = _user([{"type": "tool_result", "tool_use_id": "x", "content": "ok"}])
    p = tmp_path / "t.jsonl"
    _write(p, [_user("the real ask", origin={"kind": "human"})] + [tool] * 1500)
    assert transcript.last_human_text(str(p)) == "the real ask"


def test_recent_turns_reaches_past_long_tool_runs(tmp_path):
    tool = _user([{"type": "tool_result", "tool_use_id": "x", "content": "ok"}])
    p = tmp_path / "t.jsonl"
    _write(p, [_user("a", origin={"kind": "human"}), _user("b", origin={"kind": "human"})]
           + [tool] * 1500)
    assert transcript.recent_turns(str(p), n=2) == ["a", "b"]


def test_human_turn_timestamps_include_queued_messages(tmp_path):
    p = tmp_path / "t.jsonl"
    first = _user("first", origin={"kind": "human"})
    first["timestamp"] = "2026-11-26T03:00:00.000Z"
    _write(p, [first, _queued("second", origin={"kind": "human"},
                              ts="2026-11-26T03:17:51.273Z")])
    stamps = transcript.human_turn_timestamps(str(p), n=2)
    assert len(stamps) == 2
    assert (stamps[0] - stamps[1]).total_seconds() > 1000


def test_is_headless(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [dict(_user("run the job"), entrypoint="sdk-cli")])
    assert transcript.is_headless(str(p)) is True
    _write(p, [dict(_user("hi"), entrypoint="cli")])
    assert transcript.is_headless(str(p)) is False
    _write(p, [_user("hi")])                      # old transcript, no entrypoint
    assert transcript.is_headless(str(p)) is False
    assert transcript.is_headless(str(tmp_path / "missing.jsonl")) is True


def test_pasted_content_wrapper_is_stripped():
    # Seen 2026-11-25: a fully pasted message showed `<pasted_content id="acba">` on line 2.
    entry = _user('\n\n<pasted_content id="acba">\nI\'d assume for B Mini then merge\n'
                  '</pasted_content id="acba">\n', origin={"kind": "human"})
    assert transcript.human_text(entry) == "I'd assume for B Mini then merge"
    mixed = _user('<pasted_content id="x1">\nlog line\n</pasted_content id="x1">\n'
                  "why does this fail?", origin={"kind": "human"})
    assert transcript.human_text(mixed) == "log line\n\nwhy does this fail?"
