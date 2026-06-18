"""Day-2 data agent: cache, Elo loading/normalization/lookup, FBref shaping."""
import json
import os
import threading
import time
from core.data.cache import cached_json
from core.data import soccerdata_io as sdio


# ---------------- cache ----------------
def test_cache_uses_fresh_and_reproduces_when_expired(tmp_path):
    p = str(tmp_path / "c.json")
    calls = {"n": 0}
    def produce():
        calls["n"] += 1
        return {"v": calls["n"]}
    assert cached_json(p, 24, produce) == {"v": 1}      # produced + cached
    assert cached_json(p, 24, produce) == {"v": 1}      # served from cache
    assert calls["n"] == 1
    os.utime(p, (time.time() - 3600 * 48, time.time() - 3600 * 48))  # age it 48h
    assert cached_json(p, 24, produce) == {"v": 2}      # expired → re-produced
    assert calls["n"] == 2


def test_cache_none_path_always_produces():
    calls = {"n": 0}
    cached_json(None, 24, lambda: calls.__setitem__("n", calls["n"] + 1))
    cached_json(None, 24, lambda: calls.__setitem__("n", calls["n"] + 1))
    assert calls["n"] == 2


def test_cached_json_no_race_when_many_writers_miss_concurrently(tmp_path):
    """REGRESSION: cache.py used to hardcode `{path}.tmp` as the atomic-write
    sentinel. Two daemon workers missing the cache in the same tick both raced
    on the same tmp filename — the second `os.replace(tmp, path)` raised
    `FileNotFoundError` because the first had already consumed it. Confirmed
    in production for Switzerland T-24h (match-537335) on 2026-06-17 22:00 UTC,
    which caused `signals_failed=['dixon_coles']`.

    This test spawns many workers, all gated on a barrier so they enter the
    producer path simultaneously. The historic bug would surface as one or more
    threads raising FileNotFoundError; the fix (tempfile.mkstemp) makes the tmp
    filename unique per writer so none collide.
    """
    p = str(tmp_path / "shared.json")
    n_workers = 32
    barrier = threading.Barrier(n_workers)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    results: list[dict] = []
    results_lock = threading.Lock()

    def produce():
        # Tiny sleep widens the rename window so any race surfaces deterministically.
        time.sleep(0.01)
        return {"v": "deterministic"}

    def worker():
        try:
            barrier.wait()
            r = cached_json(p, 24, produce)
            with results_lock:
                results.append(r)
        except BaseException as e:                       # noqa: BLE001
            with errors_lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent cached_json raised: {errors!r}"
    assert len(results) == n_workers
    assert all(r == {"v": "deterministic"} for r in results)
    assert os.path.exists(p)
    with open(p) as f:
        assert json.load(f) == {"v": "deterministic"}
    # No `.tmp` files left behind under any mkstemp prefix
    leftovers = [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]
    assert not leftovers, f"leaked tmp files: {leftovers}"


# ---------------- Elo ----------------
def test_elo_loads_and_normalizes(tmp_path):
    fake = [("Korea Republic", "1700"), ("Türkiye", 1650), ("DR Congo", 1580),
            ("", 9999), ("Spain", "oops")]            # bad rows tolerated
    elo = sdio.national_team_elo(fetch=lambda: fake, cache_path=str(tmp_path / "e.json"))
    assert elo["South Korea"] == 1700.0                # normalized + float
    assert elo["Congo DR"] == 1580.0
    assert "" not in elo and "Spain" not in elo        # empty name / bad value dropped


def test_elo_lookup_defaults_for_unknown():
    elo = {"South Korea": 1700.0}
    assert sdio.elo_of(elo, "Korea Republic") == 1700.0   # normalized lookup
    assert sdio.elo_of(elo, "Nowhere FC") == sdio.DEFAULT_ELO
    h, a = sdio.match_elos(elo, "Korea Republic", "Nowhere FC")
    assert (h, a) == (1700.0, sdio.DEFAULT_ELO)


# ---------------- FBref ----------------
def test_fbref_shapes_and_normalizes(tmp_path):
    rows = [{"team": "Cabo Verde", "xg_for": 1.2, "xg_against": 1.4, "matches": 3},
            {"team": "Türkiye", "xg_for": "1.8", "xg_against": None, "matches": "2"}]
    stats = sdio.fbref_team_stats(read=lambda: rows, cache_path=str(tmp_path / "f.json"))
    assert stats["Cape Verde"]["xg_for"] == 1.2 and stats["Cape Verde"]["matches"] == 3
    assert stats["Türkiye"]["xg_for"] == 1.8 and stats["Türkiye"]["xg_against"] == 0.0


def test_live_eloratings_fetcher_is_wired(monkeypatch):
    """The live fetcher is now wired to eloratings.net/World.tsv. Patch the
    underlying fetcher to avoid hitting the network; verify the public path
    returns shaped rows. The detailed parsing is covered in test_data_wiring.py."""
    monkeypatch.setattr(
        "core.data.soccerdata_io._fetch_eloratings",
        lambda: [("Spain", 2155.0), ("France", 2062.0)],
    )
    elo = sdio.national_team_elo(fetch=None, cache_path=None)
    assert elo["Spain"] == 2155.0 and elo["France"] == 2062.0
