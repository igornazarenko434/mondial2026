"""API-Football client — backup fixtures + the lineups/injuries source.

Free tier: 100 req/day. Used at T-60m/T-15m for confirmed XI, injuries and
suspensions (feeds the news agent), and as a fallback fixtures source via
`core.reliability.with_fallback(football_data..., api_football...)`.

Scaffold — implement on Day 4/8. Wrap calls in obs.external_call("api_football", ...).
"""
from __future__ import annotations
import os

API_BASE = "https://v3.football.api-sports.io"


def _headers():
    key = os.environ.get("API_FOOTBALL_KEY")
    if not key:
        raise RuntimeError("Set API_FOOTBALL_KEY in .env")
    return {"x-apisports-key": key}


def lineups(fixture_id: int) -> dict:
    """TODO(day8): GET /fixtures/lineups?fixture=... -> confirmed XI per team.
    Wrap in obs.external_call('api_football','lineups')."""
    raise NotImplementedError


def injuries(team_id: int, season: int = 2026) -> list:
    """TODO(day8): GET /injuries?team=...&season=... -> injury/suspension list."""
    raise NotImplementedError


def fixtures_backup() -> list[dict]:
    """TODO(day4): GET /fixtures for the WC as a fallback when football-data is down.
    Return the same shape as football_data.fetch_wc_matches()."""
    raise NotImplementedError
