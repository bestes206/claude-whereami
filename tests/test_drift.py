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
