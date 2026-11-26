import pytest

from whereami import cache


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    """Safety net: this repo is editable-installed into the global venv, so a
    test that forgets its own CACHE_DIR monkeypatch must never read or write
    the real ~/.claude/whereami (live session caches). Tests that patch
    CACHE_DIR themselves simply override this."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache-guard")


@pytest.fixture(autouse=True)
def _no_real_compute_spawn(monkeypatch):
    """Safety net: a real spawn starts a detached `python -m whereami.drift`
    child that uses the REAL cache dir and calls the REAL `claude` CLI (an
    old peek test did exactly that, leaving s1.json in ~/.claude/whereami).
    Tests that exercise spawning patch _spawn_compute themselves."""
    from whereami import drift

    def _refuse(session_id, transcript_path):
        raise AssertionError("test tried to spawn a real compute child")

    monkeypatch.setattr(drift, "_spawn_compute", _refuse)
