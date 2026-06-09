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
