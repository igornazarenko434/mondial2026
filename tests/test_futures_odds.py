"""Day-7 futures-odds fetcher: HTTP mocked; verify normalization, budget
guard, and graceful handling when topscorer market is missing."""
from __future__ import annotations
from unittest.mock import patch, MagicMock
import pytest
import core.data.futures_odds as fo


_SAMPLE_WINNER_EVENT = [{
    "id": "wc-winner",
    "sport_key": "soccer_fifa_world_cup_winner",
    "bookmakers": [
        {"key": "pinnacle", "markets": [{"key": "outrights", "outcomes": [
            {"name": "Spain",         "price": 4.50},
            {"name": "France",        "price": 5.50},
            {"name": "Korea Republic","price": 75.0},   # alias → "South Korea"
            {"name": "Cape Verde Islands", "price": 250.0},  # alias → "Cape Verde"
        ]}]},
        {"key": "betfair_ex_eu", "markets": [{"key": "outrights", "outcomes": [
            {"name": "Spain",         "price": 4.30},   # shorter than pinnacle
            {"name": "France",        "price": 5.80},
        ]}]},
    ],
}]


def test_fetch_winner_outright_picks_shortest_odds_across_books(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "tok")
    monkeypatch.setattr(fo, "_budget_clear", lambda: True)
    with patch.object(fo, "_fetch_outrights", return_value=_SAMPLE_WINNER_EVENT):
        out = fo.fetch_winner_outright()
    assert out is not None
    # Betfair was sharper for Spain (4.30 < 4.50) → pick that
    assert out["Spain"] == 4.30
    # France: betfair 5.80 vs pinnacle 5.50 → pinnacle wins
    assert out["France"] == 5.50


def test_fetch_winner_outright_canonicalizes_names(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "tok")
    monkeypatch.setattr(fo, "_budget_clear", lambda: True)
    with patch.object(fo, "_fetch_outrights", return_value=_SAMPLE_WINNER_EVENT):
        out = fo.fetch_winner_outright()
    assert "South Korea" in out      # was "Korea Republic"
    assert "Cape Verde" in out       # was "Cape Verde Islands"
    assert "Korea Republic" not in out
    assert "Cape Verde Islands" not in out


def test_fetch_winner_outright_returns_none_when_over_budget(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "tok")
    monkeypatch.setattr(fo, "_budget_clear", lambda: False)
    with patch.object(fo, "_fetch_outrights") as m:
        out = fo.fetch_winner_outright()
    assert out is None
    m.assert_not_called()


def test_fetch_winner_outright_returns_none_on_empty_response(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "tok")
    monkeypatch.setattr(fo, "_budget_clear", lambda: True)
    with patch.object(fo, "_fetch_outrights", return_value=[]):
        assert fo.fetch_winner_outright() is None


def test_fetch_topscorer_outright_returns_none_when_market_not_listed(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "tok")
    monkeypatch.setattr(fo, "_resolve_topscorer_key", lambda: None)
    # _fetch_outrights should not be called
    with patch.object(fo, "_fetch_outrights") as m:
        out = fo.fetch_topscorer_outright()
    assert out is None
    m.assert_not_called()


def test_fetch_topscorer_outright_returns_dict_when_market_listed(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "tok")
    monkeypatch.setattr(fo, "_resolve_topscorer_key",
                         lambda: "soccer_fifa_world_cup_topscorer")
    monkeypatch.setattr(fo, "_budget_clear", lambda: True)
    sample = [{"bookmakers": [{"key": "pinnacle",
                "markets": [{"key": "outrights", "outcomes": [
                    {"name": "Kylian Mbappé", "price": 7.5},
                    {"name": "Harry Kane",    "price": 9.0},
                ]}]}]}]
    with patch.object(fo, "_fetch_outrights", return_value=sample):
        out = fo.fetch_topscorer_outright()
    assert out == {"Kylian Mbappé": 7.5, "Harry Kane": 9.0}


def test_fetch_topscorer_outright_returns_none_on_fetch_failure(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "tok")
    monkeypatch.setattr(fo, "_resolve_topscorer_key",
                         lambda: "soccer_fifa_world_cup_topscorer")
    def boom(*a, **k): raise RuntimeError("API down")
    monkeypatch.setattr(fo, "_fetch_outrights", boom)
    assert fo.fetch_topscorer_outright() is None
