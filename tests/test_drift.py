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
