"""Day-8 API-Football client — offline (mocked HTTP) coverage of every public
function, including the budget guard short-circuit and graceful no-data paths."""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest
import core.data.api_football as af


# ---------- helpers ----------

def _resp(json_body, ok=True, status=200):
    r = MagicMock()
    r.ok = ok
    r.status_code = status
    r.json.return_value = json_body
    r.raise_for_status = MagicMock()
    if not ok:
        r.raise_for_status.side_effect = RuntimeError(f"HTTP {status}")
    return r


# ---------- find_fixture_id ----------

def test_find_fixture_id_matches_by_canonical_names_and_date(monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "tok")
    body = {"response": [
        {"fixture": {"id": 987654, "date": "2026-06-11T19:00:00Z"},
         "teams": {"home": {"name": "Korea Republic"},   # alias
                   "away": {"name": "Cape Verde Islands"}}},  # alias
    ]}
    with patch("core.data.api_football.requests.get", return_value=_resp(body)):
        fid = af.find_fixture_id("South Korea", "Cape Verde",
                                  "2026-06-11T19:00:00+00:00")
    assert fid == 987654


def test_find_fixture_id_returns_none_when_no_match(monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "tok")
    body = {"response": [
        {"fixture": {"id": 1, "date": "2026-06-11T19:00:00Z"},
         "teams": {"home": {"name": "Iceland"},
                   "away": {"name": "Sweden"}}}]}
    with patch("core.data.api_football.requests.get", return_value=_resp(body)):
        assert af.find_fixture_id("Mexico", "South Africa",
                                    "2026-06-11T19:00:00+00:00") is None


def test_find_fixture_id_returns_none_on_empty_season(monkeypatch):
    """WC 2026 wasn't populated yet at session time — graceful None."""
    monkeypatch.setenv("API_FOOTBALL_KEY", "tok")
    with patch("core.data.api_football.requests.get",
                return_value=_resp({"response": []})):
        assert af.find_fixture_id("Mexico", "South Africa",
                                    "2026-06-11T19:00:00+00:00") is None


def test_find_fixture_id_returns_none_on_bad_kickoff_string(monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "tok")
    assert af.find_fixture_id("X", "Y", "not-a-date") is None
    assert af.find_fixture_id("X", "Y", "") is None
    assert af.find_fixture_id("X", "Y", None) is None


# ---------- fetch_lineups ----------

def test_fetch_lineups_parses_formation_and_xi(monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "tok")
    body = {"response": [
        {"team": {"name": "Mexico"}, "formation": "4-3-3",
         "coach": {"name": "Aguirre"},
         "startXI": [
             {"player": {"name": "Ochoa", "pos": "G"}},
             {"player": {"name": "Galindo", "pos": "D"}},
         ],
         "substitutes": [
             {"player": {"name": "Sub1", "pos": "M"}},
         ]},
    ]}
    with patch("core.data.api_football.requests.get", return_value=_resp(body)):
        out = af.fetch_lineups(123456)
    assert len(out) == 1
    L = out[0]
    assert L["team"] == "Mexico"
    assert L["formation"] == "4-3-3"
    assert L["coach"] == "Aguirre"
    assert "Ochoa (G)" in L["startXI"]
    assert "Sub1 (M)" in L["substitutes"]


def test_fetch_lineups_returns_none_when_not_yet_published(monkeypatch):
    """Lineups publish ~1h before kickoff. Earlier than that, response is empty."""
    monkeypatch.setenv("API_FOOTBALL_KEY", "tok")
    with patch("core.data.api_football.requests.get",
                return_value=_resp({"response": []})):
        assert af.fetch_lineups(999999) is None


def test_fetch_lineups_returns_none_for_zero_or_none_fixture():
    assert af.fetch_lineups(0) is None
    assert af.fetch_lineups(None) is None


# ---------- fetch_injuries ----------

def test_fetch_injuries_parses_player_and_reason(monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "tok")
    body = {"response": [
        {"player": {"name": "Mbappé", "position": "Attacker",
                     "type": "Knock", "reason": "Hamstring"},
         "fixture": {"date": "2026-06-26"}},
        {"player": {"name": "Kanté", "position": "Midfielder",
                     "type": "Suspension", "reason": "Yellow accumulated"}},
    ]}
    with patch("core.data.api_football.requests.get", return_value=_resp(body)):
        inj = af.fetch_injuries(771)
    assert len(inj) == 2
    assert inj[0]["player"] == "Mbappé"
    assert inj[0]["reason"] == "Hamstring"
    assert inj[1]["player"] == "Kanté"


# ---------- budget guard ----------

def test_get_short_circuits_when_over_budget(monkeypatch):
    monkeypatch.setenv("API_FOOTBALL_KEY", "tok")
    monkeypatch.setattr(af, "_budget_clear", lambda: False)
    with patch("core.data.api_football.requests.get") as g:
        out = af.fetch_lineups(123)
    g.assert_not_called()
    assert out is None
