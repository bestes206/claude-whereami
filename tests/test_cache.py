import json
from whereami import cache


def test_load_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    assert cache.load_cache("sess-1") == {}


def test_save_then_load_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache.save_cache("sess-1", {"score": 50, "turns_seen": 3})
    assert cache.load_cache("sess-1") == {"score": 50, "turns_seen": 3}


def test_save_creates_dir_and_is_session_scoped(tmp_path, monkeypatch):
    nested = tmp_path / "whereami"
    monkeypatch.setattr(cache, "CACHE_DIR", nested)
    cache.save_cache("a", {"score": 1})
    cache.save_cache("b", {"score": 2})
    assert cache.load_cache("a") == {"score": 1}
    assert cache.load_cache("b") == {"score": 2}


def test_load_corrupt_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    (tmp_path / "sess-1.json").write_text("{not json")
    assert cache.load_cache("sess-1") == {}


def test_concurrent_writers_do_not_race(tmp_path, monkeypatch):
    # Two Stop-hook processes (or a hook + a detached compute) can write the
    # same session's cache at once. A shared temp filename makes os.replace
    # fail with FileNotFoundError for the loser. Each writer must use its own
    # temp file so concurrent saves are safe (last write wins).
    import threading

    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    errors = []

    def worker():
        try:
            for _ in range(50):
                cache.save_cache("sess", {"score": 1})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], "concurrent save_cache raised: {}".format(errors)
    assert cache.load_cache("sess") == {"score": 1}


def test_load_turns_missing_or_garbage_is_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    assert cache.load_turns("s1") == 0
    (tmp_path / "s1.turns").write_text("not a number")
    assert cache.load_turns("s1") == 0
    (tmp_path / "s1.turns").write_text("")
    assert cache.load_turns("s1") == 0


def test_increment_turns_counts_up_and_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    assert cache.increment_turns("s1") == 1
    assert cache.increment_turns("s1") == 2
    assert cache.load_turns("s1") == 2


def test_save_turns_is_atomic_replace_no_tmp_left(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache.save_turns("s1", 7)
    assert cache.load_turns("s1") == 7
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_paths_share_sanitization(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    sid = "a/b"
    assert cache.turns_path(sid).name == "a_b.turns"
    assert cache.marker_path(sid).name == "a_b.computing"
    assert cache.peek_path().name == "peek"


def test_turns_at_last_compute_tolerates_garbage():
    # The .json file is hand-editable: a corrupt counter must read as 0,
    # never raise out of the hook or renderer.
    assert cache.turns_at_last_compute({}) == 0
    assert cache.turns_at_last_compute({"turns_at_last_compute": 7}) == 7
    assert cache.turns_at_last_compute({"turns_at_last_compute": "nine"}) == 0
    assert cache.turns_at_last_compute({"turns_at_last_compute": True}) == 0
    assert cache.turns_at_last_compute({"turns_at_last_compute": 7.5}) == 0
    assert cache.turns_at_last_compute({"turns_at_last_compute": None}) == 0


def test_ts_to_epoch():
    assert cache.ts_to_epoch(None) is None
    assert cache.ts_to_epoch("") is None
    assert cache.ts_to_epoch("garbage") is None
    assert cache.ts_to_epoch(12345) is None
    e1 = cache.ts_to_epoch("2026-06-09T10:00:00-07:00")
    e2 = cache.ts_to_epoch("2026-06-09T10:05:00-07:00")
    assert e1 is not None and e2 is not None and e2 - e1 == 300


def test_load_non_dict_json_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    (tmp_path / "sess-1.json").write_text("[1, 2, 3]")
    assert cache.load_cache("sess-1") == {}
    (tmp_path / "sess-1.json").write_text('"just a string"')
    assert cache.load_cache("sess-1") == {}


def test_caps_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache.save_caps({"cli_version": "2.1.172", "stripped_ok": True})
    assert cache.load_caps() == {"cli_version": "2.1.172", "stripped_ok": True}


def test_caps_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    assert cache.load_caps() == {}


def test_caps_corrupt_or_non_dict_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache.caps_path().write_text("{not json")
    assert cache.load_caps() == {}
    cache.caps_path().write_text("[1, 2]")
    assert cache.load_caps() == {}


def test_caps_save_leaves_no_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    cache.save_caps({"cli_version": "x", "stripped_ok": False})
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert cache.caps_path().name == "capabilities.json"
