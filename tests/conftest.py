import pytest

from whereami import cache


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    """Safety net: this repo is editable-installed into the global venv, so a
    test that forgets its own CACHE_DIR monkeypatch must never read or write
    the real ~/.claude/whereami (live session caches). Tests that patch
    CACHE_DIR themselves simply override this."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache-guard")
