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
