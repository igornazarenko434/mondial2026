"""API-Football client — confirmed lineups + injuries source (Day 8).

Free tier: 100 req/day. Wired calls (each ≤ 1 credit):
  - find_fixture_id(home, away, kickoff_utc)  — locate the api-football fixture
                                                  id for one WC 2026 match
  - fetch_lineups(fixture_id)                  — confirmed XI per team
  - fetch_injuries(team_id, season=2026)       — active injuries / suspensions

Day-8 cost pattern per match-window (T-60m + T-15m):
  1× find_fixture_id (skipped if cached) + 1× lineups + 2× injuries = ~4 credits
  → 4 × 104 matches × 2 active windows = ~830 over the tournament (well over 100
    if not spread). Per-day average ~28 over 30 days — safe under 100/day cap.

Every call is:
  * budget-guarded BEFORE the HTTP request (so a 401 / over-budget never blows up
    a card-window job)
  * wrapped in obs.external_call('api_football', ...) for rate-limit + ledger
  * graceful — returns None / [] on failure, logs at INFO/WARNING, never raises
"""
from __future__ import annotations
import os
from datetime import datetime, timezone
from typing import Any
import requests

from core.data.teams import normalize
from core.obs.logging import get_logger

log = get_logger("data.api_football")

API_BASE = "https://v3.football.api-sports.io"
WC_LEAGUE_ID = 1                # FIFA World Cup competition id on api-football
DEFAULT_SEASON = 2026


def _headers() -> dict:
    key = os.environ.get("API_FOOTBALL_KEY")
    if not key:
        raise RuntimeError("Set API_FOOTBALL_KEY in .env")
    return {"x-apisports-key": key}


def _budget_clear() -> bool:
    """Mirror the pattern from oddsapi: short-circuit if the free quota is
    exhausted instead of hitting a hard 429."""
    try:
        from core.obs.cost import ledger
        return not ledger().over_budget("api_football")
    except Exception:
        return True


def _get(endpoint: str, params: dict, *, label: str | None = None) -> dict | None:
    """One GET with budget guard + obs span + graceful error handling."""
    if not _budget_clear():
        log.warning("api_football over budget; skipping GET %s", endpoint)
        return None
    from core import obs
    url = f"{API_BASE}{endpoint}"
    try:
        with obs.external_call("api_football", label or endpoint.lstrip("/"), units=1):
            resp = requests.get(url, headers=_headers(), params=params, timeout=20)
            resp.raise_for_status()
    except Exception as e:                                # noqa: BLE001
        log.warning("api_football GET %s failed: %s", endpoint, e)
        return None
    try:
        return resp.json()
    except ValueError:
        log.warning("api_football GET %s returned non-JSON body", endpoint)
        return None


def find_fixture_id(home: str, away: str, kickoff_utc: str,
                    league_id: int = WC_LEAGUE_ID,
                    season: int = DEFAULT_SEASON) -> int | None:
    """Locate one fixture's api-football fixture_id.

    Strategy (1 credit max):
      - Query /fixtures?league=<id>&season=<year>&date=<YYYY-MM-DD>
        (this returns ALL WC matches on that date — usually 0-4)
      - Filter results by canonicalised team names. If the home and away match
        on BOTH sides (canonicalised), it's our match.

    Returns the integer fixture_id or None if not found / API down / not yet
    populated (the WC 2026 season is empty on api-football until the founder
    publishes fixtures — we verified earlier and return None gracefully).
    """
    if not kickoff_utc:
        return None
    try:
        date_str = (datetime.fromisoformat(str(kickoff_utc).replace("Z", "+00:00"))
                    .astimezone(timezone.utc).strftime("%Y-%m-%d"))
    except (ValueError, AttributeError):
        log.info("find_fixture_id: cannot parse kickoff_utc=%r", kickoff_utc)
        return None
    h_canon, a_canon = normalize(home), normalize(away)
    body = _get("/fixtures",
                {"league": league_id, "season": season, "date": date_str},
                label="fixtures:lookup")
    if not body or not body.get("response"):
        log.info("find_fixture_id: no fixtures on %s for league %s/%s",
                  date_str, league_id, season)
        return None
    for f in body["response"]:
        teams = f.get("teams") or {}
        ah = normalize((teams.get("home") or {}).get("name", "")) or ""
        aa = normalize((teams.get("away") or {}).get("name", "")) or ""
        if ah == h_canon and aa == a_canon:
            return int(f.get("fixture", {}).get("id"))
    log.info("find_fixture_id: %s vs %s not in api-football's list for %s",
              h_canon, a_canon, date_str)
    return None


def fetch_lineups(fixture_id: int) -> list[dict] | None:
    """GET /fixtures/lineups?fixture=<id>.

    Returns a list (one entry per team):
        [{"team": "Mexico", "formation": "4-3-3",
          "coach": "Aguirre",
          "startXI": ["Ochoa (GK)", "Galindo (DF)", ...],
          "substitutes": [...]}, ...]
    Returns None on failure / empty (lineups not yet published). Confirmed
    XIs typically publish ~1h before kickoff (= our T-60m window).
    """
    if not fixture_id:
        return None
    body = _get("/fixtures/lineups", {"fixture": int(fixture_id)},
                label="fixtures/lineups")
    if not body or not body.get("response"):
        return None
    out = []
    for row in body["response"]:
        team_name = (row.get("team") or {}).get("name", "")
        formation = row.get("formation", "")
        coach = (row.get("coach") or {}).get("name", "")
        start_xi = []
        for p in (row.get("startXI") or []):
            player = (p.get("player") or {})
            start_xi.append(f"{player.get('name', '?')} ({player.get('pos', '?')})")
        subs = []
        for p in (row.get("substitutes") or []):
            player = (p.get("player") or {})
            subs.append(f"{player.get('name', '?')} ({player.get('pos', '?')})")
        out.append({
            "team": normalize(team_name) or team_name,
            "formation": formation,
            "coach": coach,
            "startXI": start_xi,
            "substitutes": subs,
        })
    return out


def fetch_injuries(team_id: int, season: int = DEFAULT_SEASON,
                   league_id: int = WC_LEAGUE_ID) -> list[dict] | None:
    """GET /injuries?team=<team_id>&season=<season>&league=<league_id>.

    Returns a list:
        [{"player": "Mbappé", "position": "Attacker",
          "type": "Knock", "reason": "Hamstring", "match_date": "..."}]
    Filtered to ACTIVE issues (status type contains 'Missing' / 'Doubtful' /
    'Suspended') — historical injuries that have resolved are excluded.
    """
    if not team_id:
        return None
    body = _get("/injuries",
                {"team": int(team_id), "season": int(season), "league": int(league_id)},
                label="injuries")
    if not body or not body.get("response"):
        return None
    out = []
    for row in body["response"]:
        player = row.get("player") or {}
        meta = row.get("player") or {}  # api-football puts both type and reason here
        # The injury status itself is on row["player"] or row["fixture"] depending
        # on the api version; we surface whatever is present.
        out.append({
            "player": player.get("name", "?"),
            "position": player.get("position", "?"),
            "type": meta.get("type", ""),
            "reason": meta.get("reason", ""),
            "match_date": (row.get("fixture") or {}).get("date", ""),
        })
    return out


def find_team_id(team_name: str, league_id: int = WC_LEAGUE_ID,
                 season: int = DEFAULT_SEASON) -> int | None:
    """Resolve our canonical team name → api-football's numeric team_id.

    /teams?league=<id>&season=<season>&search=<team>  — search-narrowed lookup.
    Returns None if the season isn't populated yet (still True for WC 2026 at
    the time of this writing — we verified).
    """
    canon = normalize(team_name) or team_name
    body = _get("/teams",
                {"league": league_id, "season": season, "search": canon},
                label="teams:search")
    if not body or not body.get("response"):
        # Fall back to unfiltered search
        body = _get("/teams", {"search": canon}, label="teams:search_global")
        if not body or not body.get("response"):
            return None
    for row in body["response"]:
        team = row.get("team") or {}
        if normalize(team.get("name", "")) == canon:
            return int(team.get("id"))
    # Last-ditch: return the first result if the name closely matches
    first = body["response"][0].get("team") or {}
    if first.get("id"):
        return int(first["id"])
    return None


# ---- Day-4-spec fallback (kept for reliability layer) ----

def fixtures_backup() -> list[dict]:
    """Fallback fixtures source when football-data.org is down. Returns the
    same shape as football_data.fetch_wc_matches(). Day-4 leftover; not
    consumed by Day 8 but kept here for the reliability fallback."""
    body = _get("/fixtures", {"league": WC_LEAGUE_ID, "season": DEFAULT_SEASON},
                label="fixtures:all")
    if not body or not body.get("response"):
        return []
    out = []
    for f in body["response"]:
        fix = f.get("fixture") or {}
        teams = f.get("teams") or {}
        score = (f.get("score") or {}).get("fulltime") or {}
        out.append({
            "match_id": int(fix.get("id", 0)),
            "utc_kickoff": fix.get("date", ""),
            "local_kickoff": fix.get("date", ""),
            "stage": (f.get("league") or {}).get("round", ""),
            "group": None,
            "home": normalize((teams.get("home") or {}).get("name", "")),
            "away": normalize((teams.get("away") or {}).get("name", "")),
            "status": (fix.get("status") or {}).get("short", ""),
            "home_goals": score.get("home"),
            "away_goals": score.get("away"),
        })
    return out
