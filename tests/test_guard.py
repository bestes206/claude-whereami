import os

import pytest

from whereami import cache, drift


def _spawned(record):
    return lambda sid, path: record.append((sid, path))


def test_marker_ttl_invariant_holds():
    # Spec §5 invariant, recorded as an executable assert because both numbers
    # are tunable: a legitimately-running compute must never be shadowed by a
    # stale reclaim.
    assert drift.MARKER_TTL >= 2 * (drift.CLI_TIMEOUT + drift.SPAWN_SLOP)


def test_first_acquire_spawns_second_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    record = []
    assert drift.maybe_spawn_compute("s1", "/t", now=1000.0,
                                     spawner=_spawned(record)) is True
    assert drift.maybe_spawn_compute("s1", "/t", now=1000.0,
                                     spawner=_spawned(record)) is False
    assert len(record) == 1
    assert cache.marker_path("s1").exists()


def test_stale_marker_is_reclaimed_and_respawns(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    record = []
    marker = cache.marker_path("s1")
    marker.touch()
    os.utime(str(marker), (1000.0, 1000.0))
    later = 1000.0 + drift.MARKER_TTL + 1
    assert drift.maybe_spawn_compute("s1", "/t", now=later,
                                     spawner=_spawned(record)) is True
    assert len(record) == 1
    assert marker.exists()                        # fresh marker re-created
    assert os.stat(str(marker)).st_mtime > 1000   # not the stale one


def test_racing_reclaimers_loser_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    record = []
    marker = cache.marker_path("s1")
    marker.touch()
    os.utime(str(marker), (1000.0, 1000.0))

    def rename_loses(src, dst):
        raise FileNotFoundError(src)   # the other racer renamed it away first

    monkeypatch.setattr(drift.os, "rename", rename_loses)
    later = 1000.0 + drift.MARKER_TTL + 1
    assert drift.maybe_spawn_compute("s1", "/t", now=later,
                                     spawner=_spawned(record)) is False
    assert record == []


def test_spawner_raise_unlinks_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)

    def boom(sid, path):
        raise OSError("popen failed")

    assert drift.maybe_spawn_compute("s1", "/t", now=1000.0, spawner=boom) is False
    # One failed spawn must not suppress retries for a full TTL.
    assert not cache.marker_path("s1").exists()


def test_compute_entry_unlinks_marker_on_early_return(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    marker = cache.marker_path("s1")
    marker.touch()
    drift._compute_entry("s1", str(tmp_path / "empty.jsonl"))  # no goal → early return
    assert not marker.exists()


def test_compute_entry_unlinks_marker_on_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    marker = cache.marker_path("s1")
    marker.touch()

    def explode(sid, path):
        raise RuntimeError("boom")

    monkeypatch.setattr(drift, "compute", explode)
    with pytest.raises(RuntimeError):
        drift._compute_entry("s1", "/t")
    assert not marker.exists()


def test_compute_entry_tolerates_missing_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    # A stale-reclaim may have renamed the marker away; finally-unlink must
    # tolerate ENOENT.
    drift._compute_entry("s1", str(tmp_path / "empty.jsonl"))  # no raise


def test_future_dated_failure_ts_is_inert(tmp_path, monkeypatch):
    # A backwards clock step (or TZ-mangled hand edit) leaves last_failure_ts
    # in the future; without a lower clamp the negative age is < FAILURE_BACKOFF
    # forever-until-then, suppressing every spawn. Mirror peek_active's clamp.
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    record = []
    cache.save_cache("s1", {"ts": "2026-06-09T10:00:00-07:00",
                            "last_failure_ts": "2026-06-09T11:00:00-07:00"})
    before_fail = cache.ts_to_epoch("2026-06-09T10:30:00-07:00")
    assert drift.maybe_spawn_compute("s1", "/t", now=before_fail,
                                     spawner=_spawned(record)) is True
    assert len(record) == 1


def test_failure_backoff_blocks_then_allows(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    record = []
    cache.save_cache("s1", {"ts": "2026-06-09T10:00:00-07:00",
                            "last_failure_ts": "2026-06-09T11:00:00-07:00"})
    fail_epoch = cache.ts_to_epoch("2026-06-09T11:00:00-07:00")
    inside = fail_epoch + drift.FAILURE_BACKOFF - 1
    assert drift.maybe_spawn_compute("s1", "/t", now=inside,
                                     spawner=_spawned(record)) is False
    assert record == []
    after = fail_epoch + drift.FAILURE_BACKOFF + 1
    assert drift.maybe_spawn_compute("s1", "/t", now=after,
                                     spawner=_spawned(record)) is True
    assert len(record) == 1


def test_success_newer_than_failure_clears_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    record = []
    cache.save_cache("s1", {"ts": "2026-06-09T12:00:00-07:00",
                            "last_failure_ts": "2026-06-09T11:00:00-07:00"})
    now = cache.ts_to_epoch("2026-06-09T12:00:30-07:00")
    assert drift.maybe_spawn_compute("s1", "/t", now=now,
                                     spawner=_spawned(record)) is True


def test_sweep_removes_old_tmp_and_markers_keeps_json_and_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    old = 1000.0
    now = old + drift.SWEEP_AGE + 10
    for name in ("a.computing", "b.json.xyz.tmp"):
        p = tmp_path / name
        p.touch()
        os.utime(str(p), (old, old))
    fresh = tmp_path / "c.computing"
    fresh.touch()
    os.utime(str(fresh), (now - 60, now - 60))
    keep = tmp_path / "s.json"
    keep.write_text("{}")
    os.utime(str(keep), (old, old))
    drift.sweep_stale_files(now=now)
    assert not (tmp_path / "a.computing").exists()
    assert not (tmp_path / "b.json.xyz.tmp").exists()
    assert fresh.exists()
    assert keep.exists()   # cache .json files are NEVER swept (v3 data source)
