"""Day-2 data agent: cache, Elo loading/normalization/lookup, FBref shaping."""
import os
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


def test_live_fetchers_raise_until_wired():
    import pytest
    with pytest.raises(NotImplementedError):
        sdio.national_team_elo(fetch=None, cache_path=None)
