"""Day-9.20: api-football caching + multi-variant team search + quota-aware
graceful degradation.
"""
from __future__ import annotations

import os
import time
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch, tmp_path):
    """Reset module-level caches so tests don't bleed into each other."""
    from core.data import api_football as af
    # Redirect the disk cache to a tmp path so tests never touch the real cache
    monkeypatch.setattr(af, "_TEAM_ID_CACHE_PATH",
                        str(tmp_path / "team_ids.json"))
    af._TEAM_ID_CACHE = None
    af._INJURIES_CACHE.clear()
    af._FIXTURE_ID_CACHE.clear()
    yield
    af._TEAM_ID_CACHE = None
    af._INJURIES_CACHE.clear()
    af._FIXTURE_ID_CACHE.clear()


# ──────────────────── find_team_id disk cache ────────────────────────────

def test_find_team_id_disk_cache_hit_doesnt_call_api(monkeypatch):
    """If team is in the disk cache, NO api-football call should fire."""
    from core.data import api_football as af
    af._TEAM_ID_CACHE = {"Mexico": 16}
    af._save_team_id_cache()
    call_count = {"n": 0}
    def fake_get(*a, **kw):
        call_count["n"] += 1
        return None
    monkeypatch.setattr(af, "_get", fake_get)
    tid = af.find_team_id("Mexico")
    assert tid == 16
    assert call_count["n"] == 0, "should NOT hit api when cached"


def test_find_team_id_cache_miss_then_persists(monkeypatch):
    from core.data import api_football as af
    def fake_get(endpoint, params, label=None):
        return {"response": [
            {"team": {"id": 16, "name": "Mexico"}}
        ]}
    monkeypatch.setattr(af, "_get", fake_get)
    tid = af.find_team_id("Mexico")
    assert tid == 16
    # Second call must hit cache, not api
    call_count = {"n": 0}
    def angry_get(*a, **kw):
        call_count["n"] += 1
        return None
    monkeypatch.setattr(af, "_get", angry_get)
    assert af.find_team_id("Mexico") == 16
    assert call_count["n"] == 0


def test_find_team_id_tries_multiple_variants_for_czechia(monkeypatch):
    """Day-9.20: 'Czechia' should match api-football's 'Czech Republic'."""
    from core.data import api_football as af
    seen_variants = []
    def fake_get(endpoint, params, label=None):
        if endpoint != "/teams":
            return None
        search = params.get("search", "")
        seen_variants.append(search)
        if search == "Czechia":
            return {"response": []}      # api-football doesn't know Czechia
        if search == "Czech Republic":
            return {"response": [
                {"team": {"id": 9999, "name": "Czech Republic"}}
            ]}
        return None
    monkeypatch.setattr(af, "_get", fake_get)
    monkeypatch.setattr("core.data.teams.normalize",
                        lambda x: {"czech republic": "Czechia"}.get(
                            (x or "").lower(), x))
    tid = af.find_team_id("Czechia")
    assert tid == 9999, f"expected 9999, got {tid}; tried: {seen_variants}"
    # Verify both variants were tried (in order)
    assert "Czechia" in seen_variants
    assert "Czech Republic" in seen_variants


# ──────────────────── fetch_injuries TTL cache ────────────────────────────

def test_fetch_injuries_caches_within_ttl(monkeypatch):
    from core.data import api_football as af
    call_count = {"n": 0}
    def fake_get(*a, **kw):
        call_count["n"] += 1
        return {"response": [
            {"player": {"name": "Player1", "position": "MF",
                         "type": "Knock", "reason": "Hamstring"},
             "fixture": {"date": ""}}
        ]}
    monkeypatch.setattr(af, "_get", fake_get)
    monkeypatch.setattr(af, "INJURIES_TTL_SEC", 60)
    r1 = af.fetch_injuries(16)
    r2 = af.fetch_injuries(16)
    r3 = af.fetch_injuries(16)
    assert call_count["n"] == 1, f"3 calls collapsed to {call_count['n']}"
    assert r1 == r2 == r3
    assert r1[0]["player"] == "Player1"


def test_fetch_injuries_returns_stale_when_over_budget(monkeypatch):
    """Day-9.20: if api_football quota out AND we have stale cache, RETURN
    THE STALE DATA rather than nothing. Card still gets injury context."""
    from core.data import api_football as af
    # Seed a stale entry (TTL expired)
    af._INJURIES_CACHE[16] = (time.time() - 999_999,
                               [{"player": "OldInjury"}])
    monkeypatch.setattr(af, "_budget_clear", lambda: False)
    result = af.fetch_injuries(16)
    assert result == [{"player": "OldInjury"}], \
        "stale data should be served when over budget"


def test_fetch_injuries_caches_empty_results(monkeypatch):
    """Day-9.20: empty injury list is still a valid answer; cache it so we
    don't re-query for 30 min."""
    from core.data import api_football as af
    call_count = {"n": 0}
    def fake_get(*a, **kw):
        call_count["n"] += 1
        return {"response": []}
    monkeypatch.setattr(af, "_get", fake_get)
    r1 = af.fetch_injuries(16)
    r2 = af.fetch_injuries(16)
    assert r1 == [] and r2 == []
    assert call_count["n"] == 1


# ──────────────────── find_fixture_id 12h cache ────────────────────────────

def test_find_fixture_id_caches_within_ttl(monkeypatch):
    from core.data import api_football as af
    call_count = {"n": 0}
    def fake_get(*a, **kw):
        call_count["n"] += 1
        return {"response": [
            {"fixture": {"id": 1489369},
             "teams": {"home": {"name": "Mexico"},
                        "away": {"name": "South Africa"}}}
        ]}
    monkeypatch.setattr(af, "_get", fake_get)
    fid1 = af.find_fixture_id("Mexico", "South Africa",
                               "2026-06-11T19:00:00+00:00")
    fid2 = af.find_fixture_id("Mexico", "South Africa",
                               "2026-06-11T19:00:00+00:00")
    assert fid1 == fid2 == 1489369
    assert call_count["n"] == 1, f"should cache; got {call_count['n']} calls"
