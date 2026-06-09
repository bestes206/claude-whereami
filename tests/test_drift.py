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
